/* sem_shim.c - working unnamed semaphores for libsm_api on macOS.
 *
 * Darwin has never implemented POSIX unnamed semaphores: sem_init returns -1
 * with ENOSYS and leaves the sem_t untouched. libsm_api calls it eighteen
 * times and discards the result every time, so every acquisition engine ends
 * up waiting on a semaphore that was never created.
 *
 * sem_t is an int on Darwin, four bytes, too small to hold a pointer, so we
 * store a table index in it instead. pthread mutex and condvar rather than
 * dispatch_semaphore, because dispatch deliberately aborts the process if a
 * semaphore is disposed while its count is below the initial value, and we
 * cannot control how the library tears its own down.
 *
 *   clang -dynamiclib -O2 -o sem_shim.dylib sem_shim.c
 */

#include <errno.h>
#include <pthread.h>

#define MAX_SEMS 512

typedef struct {
	pthread_mutex_t mutex;
	pthread_cond_t  cond;
	unsigned        value;
	int             in_use;
	int             ready;   /* mutex and cond are created once, never destroyed */
} slot_t;

static slot_t slots[MAX_SEMS];
static pthread_mutex_t table_lock = PTHREAD_MUTEX_INITIALIZER;

static slot_t *resolve(int *sem)
{
	int i = *sem;
	if (i < 0 || i >= MAX_SEMS || !slots[i].in_use)
		return 0;
	return &slots[i];
}

int shim_sem_init(int *sem, int pshared, unsigned value)
{
	(void)pshared;
	pthread_mutex_lock(&table_lock);
	for (int i = 0; i < MAX_SEMS; i++) {
		if (slots[i].in_use)
			continue;
		if (!slots[i].ready) {
			pthread_mutex_init(&slots[i].mutex, 0);
			pthread_cond_init(&slots[i].cond, 0);
			slots[i].ready = 1;
		}
		slots[i].value = value;
		slots[i].in_use = 1;
		pthread_mutex_unlock(&table_lock);
		*sem = i;
		return 0;
	}
	pthread_mutex_unlock(&table_lock);
	errno = ENOSPC;
	return -1;
}

int shim_sem_wait(int *sem)
{
	slot_t *s = resolve(sem);
	if (!s) {
		errno = EINVAL;
		return -1;
	}
	pthread_mutex_lock(&s->mutex);
	while (s->value == 0)
		pthread_cond_wait(&s->cond, &s->mutex);
	s->value--;
	pthread_mutex_unlock(&s->mutex);
	return 0;
}

int shim_sem_post(int *sem)
{
	slot_t *s = resolve(sem);
	if (!s) {
		errno = EINVAL;
		return -1;
	}
	pthread_mutex_lock(&s->mutex);
	s->value++;
	pthread_cond_signal(&s->cond);
	pthread_mutex_unlock(&s->mutex);
	return 0;
}

int shim_sem_destroy(int *sem)
{
	pthread_mutex_lock(&table_lock);
	slot_t *s = resolve(sem);
	if (s)
		s->in_use = 0;
	pthread_mutex_unlock(&table_lock);
	*sem = -1;
	return 0;
}
