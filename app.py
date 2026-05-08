import os
import re
import shutil
import uuid
from pathlib import Path
from flask import Flask, request, jsonify, send_file, send_from_directory, after_this_request
from flask_cors import CORS
import yt_dlp
try:
    import imageio_ffmpeg
except Exception:
    imageio_ffmpeg = None

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / 'downloads'
DOWNLOAD_DIR.mkdir(exist_ok=True)
COOKIES_FILE = BASE_DIR / 'cookies.txt'

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path='')
CORS(app)
URL_RE = re.compile(r'^https?://', re.I)

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.youtube.com/',
}

YOUTUBE_CLIENT_TRIES = [None, ['web'], ['web_safari'], ['mweb'], ['android'], ['ios']]


def ffmpeg_location():
    exe = shutil.which('ffmpeg')
    if exe:
        return exe
    if imageio_ffmpeg:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return None


def clean_error(err):
    msg = re.sub(r'\x1b\[[0-9;]*m', '', str(err))
    low = msg.lower()
    if 'requested format is not available' in low or 'format is not available' in low:
        return 'Selected HD quality is not available for this video. Choose Best quality or Preview first to see available resolutions.'
    if 'http error 403' in low or 'forbidden' in low:
        return 'YouTube blocked this stream with HTTP 403. Run run_windows.bat again to update yt-dlp, or upload cookies.txt and retry.'
    if 'ffmpeg' in low:
        return 'FFmpeg is required to merge HD video + audio. This ZIP includes imageio-ffmpeg fallback, but install FFmpeg if this continues.'
    if 'sign in' in low or 'login' in low or 'cookies' in low:
        return 'This video needs login/cookies. Upload cookies.txt from your browser, then try again.'
    return msg[-700:]


def base_opts(client=None):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'windowsfilenames': True,
        'retries': 10,
        'fragment_retries': 10,
        'file_access_retries': 5,
        'extractor_retries': 5,
        'continuedl': True,
        'concurrent_fragment_downloads': 12,
        'http_headers': BROWSER_HEADERS,
        'socket_timeout': 15,
        'force_ipv4': True,
        # Prefer real resolution first. Codec is secondary.
        'format_sort': ['res', 'height', 'fps', 'br'],
        'extract_flat': False,
    }
    ff = ffmpeg_location()
    if ff:
        opts['ffmpeg_location'] = ff
    if client:
        opts['extractor_args'] = {'youtube': {'player_client': client}}
    if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0:
        opts['cookiefile'] = str(COOKIES_FILE)
    return opts


def with_client_retries(url, opts, download=False):
    last = None
    for client in YOUTUBE_CLIENT_TRIES:
        trial = dict(opts)
        if client:
            trial['extractor_args'] = {'youtube': {'player_client': client}}
        try:
            with yt_dlp.YoutubeDL(trial) as ydl:
                return ydl.download([url]) if download else ydl.extract_info(url, download=False)
        except Exception as e:
            last = e
    raise last


def quality_band(quality):
    # None means choose the highest quality the video actually has.
    bands = {
        '4320p': (4000, 4320, '8K'),
        '2160p': (2000, 2160, '4K'),
        '1440p': (1200, 1440, '2K'),
        '1080p': (1000, 1080, '1080p HD'),
        '720p': (700, 720, '720p HD'),
        '480p': (450, 480, '480p'),
        '360p': (330, 360, '360p'),
    }
    return bands.get(quality)


def choose_video_audio_format(info, quality):
    """Return explicit format ids so yt-dlp cannot silently choose a low stream.
    For Best/Maximum, pick the highest video stream available, including 2K/4K/8K.
    For a selected quality, require that resolution band and error if unavailable.
    """
    formats = info.get('formats') or []
    videos = []
    audios = []
    for f in formats:
        fid = f.get('format_id')
        if not fid:
            continue
        vcodec = f.get('vcodec')
        acodec = f.get('acodec')
        h = f.get('height') or 0
        if vcodec and vcodec != 'none' and h:
            videos.append(f)
        if acodec and acodec != 'none' and (not vcodec or vcodec == 'none'):
            audios.append(f)

    if not videos:
        raise Exception('No video stream was found for this link.')

    band = quality_band(quality)
    candidates = videos
    label = 'maximum available quality'
    if band:
        mn, mx, label = band
        candidates = [f for f in videos if mn <= int(f.get('height') or 0) <= mx]
        if not candidates:
            available = available_heights(info)
            av = ', '.join(f'{x}p' for x in available) if available else 'none found'
            raise Exception(f'{label} is not available for this video. Available video qualities: {av}. Choose Best quality to download the maximum available quality.')

    def score_video(f):
        # Prefer exact/highest height, fps, bitrate, then common browser-friendly codecs.
        codec = (f.get('vcodec') or '').lower()
        codec_bonus = 30 if ('avc' in codec or 'h264' in codec) else 20 if ('vp9' in codec or 'av01' in codec or 'av1' in codec) else 0
        return (int(f.get('height') or 0), int(f.get('fps') or 0), float(f.get('tbr') or 0), codec_bonus)

    video = max(candidates, key=score_video)

    if audios:
        audio = max(audios, key=lambda f: (float(f.get('abr') or 0), float(f.get('tbr') or 0), f.get('ext') == 'm4a'))
        return f"{video['format_id']}+{audio['format_id']}", int(video.get('height') or 0)

    # Some sites only provide a combined stream.
    return str(video['format_id']), int(video.get('height') or 0)

def available_heights(info):
    heights = set()
    for f in info.get('formats') or []:
        h = f.get('height')
        vcodec = f.get('vcodec')
        if h and vcodec and vcodec != 'none':
            heights.add(int(h))
    return sorted(heights, reverse=True)


@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/api/health')
def health():
    return jsonify({
        'ok': True,
        'yt_dlp_version': getattr(yt_dlp.version, '__version__', 'unknown'),
        'ffmpeg': ffmpeg_location() is not None,
        'cookies': COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0
    })


@app.route('/api/info', methods=['POST'])
def info():
    data = request.get_json(force=True) or {}
    url = (data.get('url') or '').strip()
    if not URL_RE.match(url):
        return jsonify({'error': 'Paste a valid video URL.'}), 400
    opts = base_opts()
    opts.update({'skip_download': True, 'format': 'bestvideo+bestaudio/best'})
    try:
        video = with_client_retries(url, opts, download=False)
        heights = available_heights(video)
        return jsonify({
            'title': video.get('title') or 'Untitled video',
            'uploader': video.get('uploader') or video.get('channel') or 'Unknown channel',
            'duration': video.get('duration'),
            'thumbnail': video.get('thumbnail'),
            'webpage_url': video.get('webpage_url') or url,
            'heights': heights,
            'best_height': heights[0] if heights else None,
        })
    except Exception as e:
        return jsonify({'error': clean_error(e)}), 400


@app.route('/api/cookies', methods=['POST'])
def cookies():
    file = request.files.get('cookies')
    if not file:
        return jsonify({'error': 'Choose cookies.txt first.'}), 400
    file.save(COOKIES_FILE)
    return jsonify({'message': 'cookies.txt saved. Try the download again.'})


def perform_download(url, mode='video', quality='best'):
    if not URL_RE.match(url):
        return jsonify({'error': 'Paste a valid video URL.'}), 400

    job_id = uuid.uuid4().hex[:10]
    outtmpl = str(DOWNLOAD_DIR / f'%(title).170B-{job_id}.%(ext)s')
    opts = base_opts()
    opts['outtmpl'] = outtmpl

    try:
        if mode == 'audio':
            opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        else:
            # Extract format list first, choose an exact high-resolution stream, then download by format id.
            info_opts = base_opts()
            info_opts.update({'skip_download': True, 'format': 'bestvideo+bestaudio/best'})
            video_info = with_client_retries(url, info_opts, download=False)
            selected_format, selected_height = choose_video_audio_format(video_info, quality)
            opts.update({
                'format': selected_format,
                'merge_output_format': 'mp4',
                'postprocessor_args': ['-movflags', '+faststart'],
            })
        with_client_retries(url, opts, download=True)
        files = sorted(DOWNLOAD_DIR.glob(f'*-{job_id}.*'), key=lambda p: p.stat().st_mtime, reverse=True)
        files = [p for p in files if not p.name.endswith(('.part', '.ytdl', '.temp'))]
        if not files:
            return jsonify({'error': 'Download finished, but file was not found.'}), 500
        path = files[0]
        @after_this_request
        def cleanup(response):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return response
        return send_file(path, as_attachment=True, download_name=path.name)
    except Exception as e:
        return jsonify({'error': clean_error(e)}), 400


@app.route('/api/download', methods=['POST'])
def download():
    data = request.get_json(force=True) or {}
    return perform_download((data.get('url') or '').strip(), data.get('mode') or 'video', data.get('quality') or 'best')

@app.route('/api/download-file')
@app.route('/api/download-file.json')
def download_file():
    return perform_download((request.args.get('url') or '').strip(), request.args.get('mode') or 'video', request.args.get('quality') or 'best')


if __name__ == '__main__':
    print('Starting downloader at http://127.0.0.1:5000')
    print('yt-dlp version:', getattr(yt_dlp.version, '__version__', 'unknown'))
    print('FFmpeg:', ffmpeg_location() or 'not found')
    app.run(host='127.0.0.1', port=5000, debug=False)
