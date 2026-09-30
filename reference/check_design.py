"""Check a pure-Python port of the Linux SM API filter design against ref_filters.json.

Ports DSP::GetLowpassFIRTaps_64f (windowed sinc, Blackman, normalised) and
CommandList::WriteIQFilter's encoding from the aarch64 2.3.9 build, then
compares both bit for bit with what the library itself produced. The fused
multiply-adds in DSP::GetBlackmanWindow are reproduced exactly with Fraction.
Run it on the Mac: sin and cos come from the platform libm, which is the one
thing that could differ from glibc.

    python3 check_design.py ref_filters.json
"""
import json, math, struct, sys
from fractions import Fraction as F
def fma(a,b,c): return float(F(a)*F(b)+F(c))
def f32(x): return struct.unpack('<f',struct.pack('<f',x))[0]
def blackman(n):
    N1=float(n-1); out=[]
    for i in range(n):
        d8=float(i)
        c1=math.cos(d8*6.283185307179586/N1)
        w=fma(-c1,0.49656,0.42659)          # fmsub: 0.42659 - c1*0.49656, one rounding
        c2=math.cos(d8*12.566370614359172/N1)
        w=fma(c2,0.076849,w)
        out.append(w)
    return out
def design(n,fc):
    fc=f32(fc); half=n//2
    w=(fc+fc)*3.141592653589793
    taps=[]
    for k in range(-half, n-half):
        x=float(k)*w
        taps.append(1.0 if x==0.0 else math.sin(x)/x)
    win=blackman(n); taps=[t*v for t,v in zip(taps,win)]
    s=0.0
    for t in taps: s+=t
    return [t/s for t in taps]
SCALE={1:1<<19,2:1<<18,3:1<<18,4:1<<18}
def encode(stage,taps):
    s=0.0
    for t in taps: s+=t
    inv=1.0/s; half=(len(taps)+1)//2
    return [int((inv*t)*SCALE[stage]) & 0xffffffff for t in taps[:half]]
if __name__=='__main__':
    r=json.load(open(sys.argv[1] if len(sys.argv) > 1 else 'ref_filters.json'))
    tap_bad=word_bad=filt_bad=0; ulp=0; tot=0
    for f in r['filters']:
        ref=[struct.unpack('<d',bytes.fromhex(h)[::-1])[0] for h in f['coeffs']]
        mine=design(f['taps'],f['fc'])
        for a,b in zip(ref,mine):
            tot+=1
            if a!=b: tap_bad+=1; ulp=max(ulp,abs(struct.unpack('<q',struct.pack('<d',a))[0]-struct.unpack('<q',struct.pack('<d',b))[0]))
        cw=f['command'][9:]; mw=encode(f['stage'],ref); mw2=encode(f['stage'],mine)
        if cw!=mw: word_bad+=1
        if cw!=mw2: filt_bad+=1
    print(f'taps differing {tap_bad}/{tot}, worst {ulp} ulp')
    print(f'encoding from reference taps: {word_bad} of {len(r["filters"])} commands differ')
    print(f'full port (design + encoding): {filt_bad} of {len(r["filters"])} commands differ')
    sys.exit(1 if filt_bad else 0)
