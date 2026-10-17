#!/usr/bin/env python3
"""Etwal Matinal Mizik — ffmpeg renderer.

Builds a lyric video (16:9) or a Short (9:16) from a JSON spec:
  scenes (still image + optional animated clip), synced karaoke lyrics, title card, audio.
The spec is produced by the n8n workflow EMM-04 and passed in base64 (workflow_dispatch input),
or as a local file for testing:  python render.py spec.json
"""
import base64, json, os, subprocess, sys, urllib.request, shutil, math

WORK = os.environ.get('WORK', 'work')
FONT_DIR = os.environ.get('FONT_DIR', 'fonts')


def sh(cmd):
    print('+', ' '.join(cmd)[:400], flush=True)
    subprocess.run(cmd, check=True)


def probe_duration(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out or 0)


def fetch(url, dest):
    if os.path.exists(url):
        shutil.copy(url, dest)
        return dest
    req = urllib.request.Request(url, headers={'User-Agent': 'etwal-render'})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, 'wb') as f:
        shutil.copyfileobj(r, f)
    return dest


def ass_time(t):
    t = max(0.0, t)
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f'{h}:{m:02d}:{s:05.2f}'


def ass_escape(s):
    return s.replace('\\', '\\\\').replace('{', '(').replace('}', ')').replace('\n', ' ')


def build_ass(spec, path):
    W, H = spec['width'], spec['height']
    vertical = H > W
    st = spec.get('style', {})
    font = st.get('font', 'Nunito')
    title_font = st.get('title_font', 'Playfair Display')
    size = st.get('lyric_size', 70 if vertical else 76)
    margin_v = st.get('lyric_margin_v', 420 if vertical else 95)
    sung = st.get('sung_color', '&H0060D8FF')     # warm gold (BGR)
    unsung = st.get('unsung_color', '&H00FFFFFF')  # white
    lines = [
        '[Script Info]', 'ScriptType: v4.00+', f'PlayResX: {W}', f'PlayResY: {H}', 'WrapStyle: 0', 'ScaledBorderAndShadow: yes', '',
        '[V4+ Styles]',
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
        f'Style: Lyric,{font},{size},{sung},{unsung},&H00201008,&H96000000,-1,0,0,0,100,100,0,0,1,3.2,2.5,2,{int(W*0.07)},{int(W*0.07)},{margin_v},1',
        f'Style: Title,{title_font},{int(size*1.65)},&H00D6F4FF,&H00D6F4FF,&H00201008,&H96000000,-1,0,0,0,100,100,0,0,1,3.5,3,5,60,60,0,1',
        f'Style: Sub,{font},{int(size*0.62)},&H00FFFFFF,&H00FFFFFF,&H00201008,&H96000000,0,0,0,0,100,100,1,0,1,2.5,2,5,60,60,0,1',
        f'Style: Cta,{font},{int(size*0.6)},&H008AE0FF,&H008AE0FF,&H00201008,&H96000000,-1,0,0,0,100,100,0,0,1,2.5,2,2,60,60,{int(H*0.12)},1',
        '', '[Events]', 'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text'
    ]
    t = spec.get('title')
    if t and t.get('text'):
        y = int(H * (0.40 if not vertical else 0.30))
        lines.append(f"Dialogue: 1,{ass_time(t['start'])},{ass_time(t['end'])},Title,,0,0,0,,{{\\fad(900,900)\\pos({W//2},{y})}}{ass_escape(t['text'])}")
        if t.get('sub'):
            lines.append(f"Dialogue: 1,{ass_time(t['start']+0.6)},{ass_time(t['end'])},Sub,,0,0,0,,{{\\fad(900,900)\\pos({W//2},{y + int(size*1.35)})}}{ass_escape(t['sub'])}")
    for ln in spec.get('lines', []):
        show, hide = ln['show'], ln['hide']
        words = ln.get('words') or []
        if words:
            parts = []
            cur = show
            for w in words:
                lead = max(0.0, w['s'] - cur)
                if lead >= 0.01:
                    parts.append('{\\k%d}' % round(lead * 100))
                dur = max(0.08, w['e'] - max(w['s'], cur))
                parts.append('{\\kf%d}%s ' % (round(dur * 100), ass_escape(w['w'])))
                cur = max(cur, w['e'])
            text = ''.join(parts).rstrip()
        else:
            text = ass_escape(ln['text'])
        lines.append(f"Dialogue: 0,{ass_time(show)},{ass_time(hide)},Lyric,,0,0,0,,{{\\fad(220,220)}}{text}")
    c = spec.get('cta')
    if c and c.get('text'):
        lines.append(f"Dialogue: 2,{ass_time(c['start'])},{ass_time(c['end'])},Cta,,0,0,0,,{{\\fad(400,300)}}{ass_escape(c['text'])}")
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def still_segment(img, dur, W, H, fps, idx, out):
    """Slow Ken Burns on a still image."""
    frames = max(1, int(round(dur * fps)))
    moves = [
        ("1.0+0.10*on/{n}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"),               # push in
        ("1.10-0.10*on/{n}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"),              # pull out
        ("1.08", "(iw-iw/zoom)*on/{n}", "(ih-ih/zoom)/2"),                     # pan right
        ("1.08", "(iw-iw/zoom)*(1-on/{n})", "(ih-ih/zoom)/2"),                 # pan left
    ]
    z, x, y = [m.format(n=frames) for m in moves[idx % len(moves)]]
    vf = (f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={fps},format=yuv420p")
    sh(['ffmpeg', '-y', '-loglevel', 'error', '-loop', '1', '-i', img, '-vf', vf, '-frames:v', str(frames),
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-r', str(fps), out])


def clip_segment(clip, dur, W, H, fps, out):
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps},format=yuv420p"
    sh(['ffmpeg', '-y', '-loglevel', 'error', '-i', clip, '-t', f'{dur:.3f}', '-an', '-vf', vf,
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', out])


def loop_segment(clip, dur, W, H, fps, out):
    """Fill `dur` seconds with continuous motion: play the clip forward, then backward (ping-pong), repeat."""
    base = out + '.fwd.mp4'
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps},format=yuv420p"
    sh(['ffmpeg', '-y', '-loglevel', 'error', '-i', clip, '-an', '-vf', vf, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', base])
    rev = out + '.rev.mp4'
    sh(['ffmpeg', '-y', '-loglevel', 'error', '-i', base, '-vf', 'reverse', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', rev])
    cd = probe_duration(base)
    n = max(1, math.ceil(dur / max(cd, 0.5)))
    lst = out + '.txt'
    with open(lst, 'w') as f:
        for k in range(n + 1):
            f.write(f"file '{os.path.abspath(base if k % 2 == 0 else rev)}'\n")
    sh(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', lst, '-t', f'{dur:.3f}',
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-r', str(fps), out])


def xfade_concat(segs, durs, X, out, fps):
    """Concatenate segments with crossfades of X seconds (each seg already includes its overlap)."""
    if len(segs) == 1:
        shutil.copy(segs[0], out)
        return
    inputs = []
    for s in segs:
        inputs += ['-i', s]
    fc, prev, offset = [], '[0:v]', 0.0
    for i in range(1, len(segs)):
        offset += durs[i - 1] - X
        lab = f'[v{i}]'
        fc.append(f'{prev}[{i}:v]xfade=transition=fade:duration={X}:offset={offset:.3f}{lab}')
        prev = lab
    sh(['ffmpeg', '-y', '-loglevel', 'error', *inputs, '-filter_complex', ';'.join(fc), '-map', prev,
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-r', str(fps), out])


def render(spec):
    os.makedirs(WORK, exist_ok=True)
    W, H, fps = spec['width'], spec['height'], spec.get('fps', 30)
    total = float(spec['duration'])
    X = float(spec.get('crossfade', 1.0))

    # 1) one segment per scene slot (animated clip first, then the still keeps moving)
    segs, durs = [], []
    for i, sl in enumerate(spec['slots']):
        first, last = i == 0, i == len(spec['slots']) - 1
        dur = (sl['end'] - sl['start']) + (0 if last else X)
        img = fetch(sl['image'], f'{WORK}/img_{i}.png')
        parts, pdurs = [], []
        if sl.get('clip'):
            clip = fetch(sl['clip'], f'{WORK}/clip_{i}.mp4')
            loop_segment(clip, dur, W, H, fps, f'{WORK}/c_{i}.mp4')
            parts.append(f'{WORK}/c_{i}.mp4'); pdurs.append(dur)
        else:
            still_segment(img, dur, W, H, fps, i, f'{WORK}/s_{i}.mp4')
            parts.append(f'{WORK}/s_{i}.mp4'); pdurs.append(dur)
        seg = f'{WORK}/seg_{i}.mp4'
        xfade_concat(parts, pdurs, 0.6, seg, fps)
        segs.append(seg); durs.append(sum(pdurs) - 0.6 * (len(pdurs) - 1))

    base = f'{WORK}/base.mp4'
    xfade_concat(segs, durs, X, base, fps)

    # 2) readability gradient + karaoke lyrics + audio
    ass = f'{WORK}/lyrics.ass'
    build_ass(spec, ass)
    a = spec['audio']
    audio = fetch(a['url'], f'{WORK}/audio' + os.path.splitext(a['url'].split('?')[0])[1])
    g0 = 0.55 if H > W else 0.62
    grad = (f"color=c=black@0:s={W}x{H}:d=1,format=rgba,"
            f"geq=r=0:g=0:b=0:a='if(gt(Y,H*{g0}),min(185,(Y-H*{g0})/(H*{1-g0})*185),0)'")
    sh(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', grad, '-frames:v', '1', f'{WORK}/grad.png'])
    fade_out = float(a.get('fade_out', 0))
    afilt = [f"atrim=start={float(a.get('seek', 0)):.3f}:duration={total:.3f}", 'asetpts=PTS-STARTPTS']
    if a.get('fade_in'):
        afilt.append(f"afade=t=in:st=0:d={a['fade_in']}")
    if fade_out:
        afilt.append(f"afade=t=out:st={max(0, total - fade_out):.3f}:d={fade_out}")
    fo = 0.8 if H > W else 2.5   # Shorts: keep the end CTA readable
    vfin = f"fade=t=in:st=0:d=1.2,fade=t=out:st={max(0, total - fo):.3f}:d={fo}"
    fc = (f"[0:v]trim=duration={total:.3f},setpts=PTS-STARTPTS[b];[b][2:v]overlay=0:0[g];"
          f"[g]ass={ass}:fontsdir={FONT_DIR},{vfin}[v];[1:a]{','.join(afilt)}[a]")
    out = spec.get('output', f'{WORK}/out.mp4')
    sh(['ffmpeg', '-y', '-loglevel', 'error', '-i', base, '-i', audio, '-loop', '1', '-i', f'{WORK}/grad.png',
        '-filter_complex', fc, '-map', '[v]', '-map', '[a]', '-t', f'{total:.3f}',
        '-c:v', 'libx264', '-preset', spec.get('preset', 'medium'), '-crf', '19', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', out])
    return out, probe_duration(out)


MAX_UPLOAD = int(os.environ.get('MAX_UPLOAD_BYTES', str(48 * 1024 * 1024)))


def preview_copy(src, dur, dest):
    """Re-encode to fit the Supabase per-file limit (review copy). The full-quality master goes to a GitHub release."""
    if os.path.getsize(src) <= MAX_UPLOAD:
        shutil.copy(src, dest)
        return dest
    kbps = max(400, int((MAX_UPLOAD * 8 / 1000) / max(dur, 1) * 0.86) - 128)
    sh(['ffmpeg', '-y', '-loglevel', 'error', '-i', src, '-c:v', 'libx264', '-preset', 'medium', '-b:v', f'{kbps}k',
        '-maxrate', f'{int(kbps*1.4)}k', '-bufsize', f'{kbps*2}k', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart', dest])
    return dest


def upload(path, url):
    r = subprocess.run(['curl', '-sS', '-w', '\\n%{http_code}', '-X', 'PUT', '-H', 'Content-Type: video/mp4', '-H', 'x-upsert: true',
                        '--data-binary', f'@{path}', url], capture_output=True, text=True)
    code = (r.stdout.strip().splitlines() or ['0'])[-1]
    if not code.startswith('2'):
        raise RuntimeError(f'upload HTTP {code}: {r.stdout[-300:]} {r.stderr[-200:]} size={os.path.getsize(path)}')


def release_masters(tag, files):
    """Attach full-quality masters to a GitHub release in this (private) repo."""
    repo = os.environ.get('GITHUB_REPOSITORY')
    if not repo or not files:
        return None
    subprocess.run(['gh', 'release', 'delete', tag, '--repo', repo, '--yes', '--cleanup-tag'], capture_output=True)
    sh(['gh', 'release', 'create', tag, *files, '--repo', repo, '--title', tag, '--notes', 'Etwal Matinal Mizik render masters'])
    return tag


def callback(url, payload):
    if not url:
        return
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    try:
        urllib.request.urlopen(req, timeout=60).read()
    except Exception as e:  # never fail the render because of the callback
        print('callback failed:', e)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('SPEC_B64', '')
    if os.path.exists(arg):
        jobs = json.load(open(arg, encoding='utf-8'))
    else:
        jobs = json.loads(base64.b64decode(arg).decode('utf-8'))
    if isinstance(jobs, dict) and 'jobs' in jobs:
        meta, jobs = jobs, jobs['jobs']
    else:
        meta, jobs = {}, (jobs if isinstance(jobs, list) else [jobs])
    results, masters = [], []
    os.makedirs('masters', exist_ok=True)
    for job in jobs:
        global WORK
        kind = job.get('kind', 'video')
        WORK = os.path.join(os.environ.get('WORK', 'work'), kind)
        try:
            out, dur = render(job)
            master = os.path.join('masters', f'{kind}.mp4')
            shutil.copy(out, master)
            masters.append(master)
            prev = preview_copy(out, dur, os.path.join(WORK, 'preview.mp4'))
            if job.get('upload_url'):
                upload(prev, job['upload_url'])
            results.append({'kind': kind, 'path': job.get('upload_path'), 'duration': round(dur, 2), 'expect': job['duration'],
                             'status': 'done', 'master_bytes': os.path.getsize(master), 'preview_bytes': os.path.getsize(prev),
                             'master_asset': f'{kind}.mp4'})
        except Exception as e:
            results.append({'kind': kind, 'status': 'error', 'error': str(e)[:700]})
    tag = None
    try:
        label = meta.get('label') or f"{meta.get('song_id', 'song')}-{meta.get('run_ref', 'run')}"
        tag = release_masters(label, masters)
    except Exception as e:
        print('release failed:', e)
    payload = {'song_id': meta.get('song_id'), 'run_ref': meta.get('run_ref'), 'gh_run': os.environ.get('GITHUB_RUN_ID'), 'release': tag, 'results': results}
    print(json.dumps(payload, indent=1))
    callback(meta.get('callback_url'), payload)
    if any(r['status'] != 'done' for r in results):
        sys.exit(1)


if __name__ == '__main__':
    main()
