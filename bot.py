import os
import time
import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse, parse_qs, urldefrag

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_IDS = list(dict.fromkeys(x.strip() for x in os.getenv('TELEGRAM_CHAT_ID', '').split(',') if x.strip()))
VOX_URL = 'https://egy.voxcinemas.com/movies/whatson'
DATA_FILE = Path(os.getenv('VOX_DATA_FILE', str(BASE_DIR / 'seen_data.json')))
COMING_URL = 'https://egy.voxcinemas.com/movies/comingsoon'
# Set this only after confirming VOX permits automated monitoring.
MONITOR_ALLOWED = os.getenv('VOX_MONITOR_ALLOWED', 'false').lower() == 'true'
CAIRO = ZoneInfo('Africa/Cairo')

def check_interval(now=None):
    day = (now or datetime.now(CAIRO)).weekday()
    return {1: 300, 2: 300, 3: 300}.get(day, 600)

KEYWORDS = ['concert', 'live viewing', 'world tour', 'live broadcast',
            'live in cinemas', 'live performance', 'concert film', 'حفلة', 'حفل موسيقي','Hadoota']

# Exact VOX movie URL slugs to monitor in addition to concert/live-event titles.
# Both Avengers: Doomsday listings are included (regular + Infinity Vision).
TRACKED_MOVIE_SLUGS = {
    'avengers-doomsday',
    'avengers-doomsday-infinity-vision',
    'dune-part-three',
}


def send_telegram(message):
    success = True
    for index, chat_id in enumerate(CHAT_IDS, 1):
        try:
            response = requests.post(
                f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                data={'chat_id': chat_id, 'text': message}, timeout=30)
            delivered = response.ok and response.json().get('ok', False)
        except (requests.RequestException, ValueError):
            delivered = False
        print(f'Telegram recipient {index}: ' + ('delivered' if delivered else 'failed'))
        success = success and delivered
    return success


def send_telegram_long(message, limit=3800):
    """Send a long Telegram report in safe-sized chunks to every recipient."""
    if len(message) <= limit:
        return send_telegram(message)

    chunks = []
    current = ''
    for block in message.split('\n\n'):
        candidate = block if not current else current + '\n\n' + block
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            while len(block) > limit:
                chunks.append(block[:limit])
                block = block[limit:]
            current = block
    if current:
        chunks.append(current)

    success = True
    total = len(chunks)
    for index, chunk in enumerate(chunks, 1):
        success = send_telegram(f'📢 COMING SOON ({index}/{total})\n\n' + chunk) and success
    return success


def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding='utf-8'))
    return {'concerts': [], 'sessions': [], 'coming': []}


def save_data(data):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=4), encoding='utf-8')
    temporary.replace(DATA_FILE)


def get_page(url):
    """Fetch a VOX page with one cautious retry for transient failures.

    A 403 is retried once after a short pause in case it was transient.
    If VOX continues to refuse access, the error is raised; this code does
    not attempt to bypass access controls, rotate IPs, or disguise requests.
    """
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(url, timeout=30)
            status = response.status_code

            if status == 403:
                print(f'VOX returned HTTP 403 for {url} (attempt {attempt}/{max_attempts}).')
                if attempt < max_attempts:
                    time.sleep(20)
                    continue
                response.raise_for_status()

            if status == 429:
                retry_after = response.headers.get('Retry-After', '')
                try:
                    wait_seconds = min(max(int(retry_after), 5), 60)
                except (TypeError, ValueError):
                    wait_seconds = 30
                print(f'VOX rate-limited the request (429); waiting {wait_seconds}s.')
                if attempt < max_attempts:
                    time.sleep(wait_seconds)
                    continue
                response.raise_for_status()

            if 500 <= status <= 599 and attempt < max_attempts:
                print(f'VOX server error {status} for {url}; retrying in 10s.')
                time.sleep(10)
                continue

            response.raise_for_status()
            return BeautifulSoup(response.text, 'html.parser')

        except (requests.Timeout, requests.ConnectionError) as error:
            print(f'{type(error).__name__} for {url} (attempt {attempt}/{max_attempts}): {error}')
            if attempt >= max_attempts:
                raise
            time.sleep(5)

    raise RuntimeError(f'Unable to retrieve VOX page after {max_attempts} attempts: {url}')


def get_concerts(page_url=VOX_URL):
    soup = get_page(page_url)
    concerts = {}
    listing_count = 0
    for heading in soup.find_all('h3'):
        title = heading.get_text(' ', strip=True)
        link = heading.find('a', href=True)
        if not title or not link:
            continue
        url = urldefrag(urljoin(page_url, link['href']))[0]
        if urlparse(url).netloc != urlparse(VOX_URL).netloc or not urlparse(url).path.startswith('/movies/'):
            continue
        listing_count += 1
        slug = urlparse(url).path.rstrip('/').split('/')[-1].lower()
        is_concert_or_live_event = any(word in title.lower() for word in KEYWORDS)
        is_tracked_movie = slug in TRACKED_MOVIE_SLUGS
        if is_concert_or_live_event or is_tracked_movie:
            concerts[url] = title
    if not listing_count:
        raise RuntimeError('No movie listings found.')
    return concerts


def get_showtimes(movie_url):
    sessions = {}
    visited = set()
    today = datetime.now(ZoneInfo('Africa/Cairo')).date()
    movie_url = urldefrag(movie_url)[0]

    def extract_sessions(soup, page_url):
        section = soup.select_one('section#showtimes')
        if not section:
            return
        active = section.select_one('nav.date-filter li span')
        label = active.get_text(' ', strip=True) if active else ''
        query = parse_qs(urlparse(page_url).query)
        screening_date = label or 'Check booking page'
        if label.lower() == 'today':
            screening_date = today.strftime('%a %d %b %Y')
        elif label.lower() == 'tomorrow':
            screening_date = (today + timedelta(days=1)).strftime('%a %d %b %Y')
        elif 'd' in query and label:
            try:
                requested = datetime.strptime(query['d'][0], '%Y%m%d')
                if label == requested.strftime('%a %d %b'):
                    screening_date = requested.strftime('%a %d %b %Y')
            except ValueError:
                pass
        for link in section.select('a.showtime[data-id][href]'):
            cinema = link.find_previous('h3')
            fmt = link.find_previous('strong')
            sessions.setdefault(link['data-id'], {
                'date': screening_date,
                'cinema': cinema.get_text(' ', strip=True) if cinema else 'Unknown cinema',
                'time': link.get_text(' ', strip=True),
                'format': fmt.get_text(' ', strip=True) if fmt else 'Unknown format',
                'url': urljoin(page_url, link['href'])
            })

    soup = get_page(movie_url)
    visited.add(movie_url)
    extract_sessions(soup, movie_url)
    for link in soup.select('section#showtimes nav.date-filter a[href]'):
        date_url = urldefrag(urljoin(movie_url, link['href']))[0]
        parsed = urlparse(date_url)
        if (parsed.netloc != urlparse(movie_url).netloc or
                parsed.path != urlparse(movie_url).path or
                'd' not in parse_qs(parsed.query) or date_url in visited):
            continue
        print('Checking additional date:', link.get_text(' ', strip=True))
        time.sleep(3)
        extract_sessions(get_page(date_url), date_url)
        visited.add(date_url)
    for info in sessions.values():
        print(f"  {info['date']} | {info['cinema']} | {info['time']} | {info['format']}")
    print(f'Date pages checked: {len(visited)}')
    print(f'Unique sessions: {len(sessions)}')
    return sessions


def check_updates():
    first_run = not DATA_FILE.exists()
    data = load_data()
    coming_first = not data.get('coming_initialized', False)
    data.setdefault('coming', [])
    concerts = get_concerts()
    print(f'Concerts found: {len(concerts)}')
    current_sessions = {}
    for movie_url, title in concerts.items():
        print(f'Concert: {title}')
        time.sleep(2)
        current_sessions[movie_url] = get_showtimes(movie_url)
    new_concerts = new_sessions = 0
    for movie_url, title in concerts.items():
        if movie_url not in data['concerts']:
            message = f'🎬 NEW MONITORED TITLE DETECTED!\n\n🎬 {title}\n\n🔗 {movie_url}'
            if first_run or send_telegram(message):
                data['concerts'].append(movie_url)
                new_concerts += 1
                if not first_run:
                    save_data(data)
        for session_id, info in current_sessions[movie_url].items():
            if session_id in data['sessions']:
                continue
            message = (
                '🚨 NEW SHOWTIME AVAILABLE!\n\n'
                f'🎬 {title}\n'
                f"📍 {info['cinema']}\n"
                f"📅 {info['date']}\n"
                f"⏰ {info['time']}\n"
                f"🎥 {info['format']}\n\n"
                f"🎟 Open booking page:\n{info['url']}"
            )
            if first_run or send_telegram(message):
                data['sessions'].append(session_id)
                new_sessions += 1
                if not first_run:
                    save_data(data)
    save_data(data)
    print(f'New concerts: {new_concerts}')
    print(f'New sessions: {new_sessions}')
    if first_run:
        send_telegram('✅ VOX Alert Bot initialized!\n\n'
                      f"🎵 Existing concerts: {len(data['concerts'])}\n"
                      f"🎟 Existing sessions: {len(data['sessions'])}\n\n"
                      'Your monitoring baseline is ready.')

    upcoming = get_concerts(COMING_URL)
    print(f'Coming soon monitored titles: {len(upcoming)}')

    if upcoming:
        print('Coming soon titles:')
        for index, (url, title) in enumerate(upcoming.items(), 1):
            print(f'  {index}. {title}')
            print(f'     {url}')
    else:
        print('No matching monitored titles found in Coming Soon.')

    # Send a consolidated Telegram report only when the monitored Coming Soon
    # list changes. This avoids repeating the same report every scan.
    upcoming_items = sorted(
        ((url, title) for url, title in upcoming.items()),
        key=lambda item: item[1].casefold()
    )
    digest_source = "\n".join(f"{title}|{url}" for url, title in upcoming_items)
    coming_digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
    previous_digest = data.get('coming_digest')

    if coming_digest != previous_digest:
        if upcoming_items:
            lines = [
                '📢 VOX CINEMAS — COMING SOON',
                '',
                f'Found {len(upcoming_items)} monitored title(s):',
                ''
            ]
            for index, (url, title) in enumerate(upcoming_items, 1):
                lines.append(f'{index}. 🎬 {title}')
                lines.append(f'🔗 {url}')
                lines.append('')
            report = "\n".join(lines).strip()
        else:
            report = (
                '📢 VOX CINEMAS — COMING SOON\n\n'
                'No monitored titles are currently listed in Coming Soon.'
            )

        if send_telegram_long(report):
            data['coming_digest'] = coming_digest
            data['coming'] = list(upcoming.keys())
            data['coming_initialized'] = True
            save_data(data)
            print('Coming Soon Telegram report: delivered to all configured recipients.')
        else:
            print('Coming Soon Telegram report: delivery failed; it will be retried next scan.')
    else:
        print('Coming Soon list unchanged; no duplicate Telegram report sent.')

    # A successful full scan can send one heartbeat in each Cairo hour window.
    now = datetime.now(CAIRO)
    if now.hour in (0, 12):
        slot = now.strftime('%Y-%m-%d %H')
        if data.get('last_health_slot') != slot:
            message = (
                '✅ VOX BOT STATUS: ONLINE\n\n'
                f'🕒 Successful scan: {now:%d %b %Y %I:%M %p} Cairo\n'
                f'🎵 Current concerts: {len(concerts)}\n'
                f'🎟 Current sessions: {sum(len(x) for x in current_sessions.values())}\n'
                f'📢 Coming soon concerts: {len(upcoming)}\n'
                f'🆕 New sessions this scan: {new_sessions}\n'
                'Website scan completed successfully.'
            )
            if send_telegram(message):
                data['last_health_slot'] = slot
                save_data(data)
            else:
                raise RuntimeError('Health notification delivery failed.')


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='Run one scan and exit')
    parser.add_argument('--test-telegram', action='store_true')
    args = parser.parse_args()
    if not TOKEN or not CHAT_IDS:
        raise SystemExit('Missing Telegram credentials.')
    if args.test_telegram:
        if not send_telegram('✅ VOX Alert Bot v4: Telegram test successful.'):
            raise SystemExit('Telegram test failed.')
        return
    if not args.once and not MONITOR_ALLOWED:
        raise SystemExit('Confirm VOX permits automated monitoring, then set VOX_MONITOR_ALLOWED=true in .env.')
    print('VOX ALERT BOT V4 STARTED')
    print('Press Ctrl+C to stop.')
    failures = 0
    warned = False
    while True:
        print('\n' + '=' * 45)
        print(datetime.now(CAIRO).strftime('%Y-%m-%d %H:%M:%S %Z'))
        try:
            check_updates()
            if warned:
                if send_telegram('✅ VOX monitoring recovered.'):
                    warned = False
            failures = 0
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            print(f'Website HTTP error: {status}')
            if status in (401, 403, 429):
                send_telegram(f'⚠️ VOX monitoring stopped: HTTP {status}. Check access permissions before restarting.')
                raise SystemExit(1)
            failures += 1
        except Exception as error:
            print(f'Check failed: {type(error).__name__}: {error}')
            failures += 1
        if failures >= 2 and not warned:
            warned = send_telegram('⚠️ VOX monitoring has failed repeatedly. Retrying with backoff.')
        if args.once:
            if failures:
                raise SystemExit(1)
            return
        delay = max(check_interval(), min(3600, 300 * 2 ** min(failures, 4))) if failures else check_interval()
        print(f'Next check in {delay} seconds.')
        time.sleep(delay)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nBot stopped.')
