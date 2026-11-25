import os
import time
import json
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
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
VOX_URL = 'https://egy.voxcinemas.com/movies/whatson'
DATA_FILE = Path(os.getenv('VOX_DATA_FILE', str(BASE_DIR / 'seen_data.json')))
COMING_URL = 'https://egy.voxcinemas.com/movies/comingsoon'
# Set this only after confirming VOX permits automated monitoring.
MONITOR_ALLOWED = os.getenv('VOX_MONITOR_ALLOWED', 'false').lower() == 'true'
CAIRO = ZoneInfo('Africa/Cairo')

def check_interval(now=None):
    day = (now or datetime.now(CAIRO)).weekday()
    return {1: 300, 2: 60, 3: 300}.get(day, 600)

KEYWORDS = ['concert', 'live viewing', 'world tour', 'live broadcast',
            'live in cinemas', 'live performance', 'concert film', 'حفلة', 'حفل موسيقي']


def send_telegram(message):
    try:
        response = requests.post(
            f'https://api.telegram.org/bot{TOKEN}/sendMessage',
            data={'chat_id': CHAT_ID, 'text': message}, timeout=30)
        return response.ok and response.json().get('ok', False)
    except (requests.RequestException, ValueError):
        print('Telegram notification failed.')
        return False


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
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


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
        if any(word in title.lower() for word in KEYWORDS):
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
            message = f'🎵 NEW CONCERT DETECTED!\n\n🎬 {title}\n\n🔗 {movie_url}'
            if first_run or send_telegram(message):
                data['concerts'].append(movie_url)
                new_concerts += 1
                if not first_run:
                    save_data(data)
        for session_id, info in current_sessions[movie_url].items():
            if session_id in data['sessions']:
                continue
            message = (
                '🚨 NEW CONCERT SHOWTIME!\n\n'
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

    time.sleep(3)
    upcoming = get_concerts(COMING_URL)
    print(f'Coming soon concerts: {len(upcoming)}')
    for url, title in upcoming.items():
        if url in data['coming']:
            continue
        if coming_first or url in data['concerts'] or send_telegram(
                f'📢 UPCOMING CONCERT ANNOUNCED!\n\n🎬 {title}\n'
                f'Tickets may not be available yet.\n🔗 {url}'):
            data['coming'].append(url)
            save_data(data)
    if coming_first:
        data['coming_initialized'] = True
        save_data(data)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='Run one scan and exit')
    parser.add_argument('--test-telegram', action='store_true')
    args = parser.parse_args()
    if not TOKEN or not CHAT_ID:
        raise SystemExit('Missing Telegram credentials.')
    if args.test_telegram:
        if not send_telegram('✅ VOX Alert Bot v3: Telegram test successful.'):
            raise SystemExit('Telegram test failed.')
        return
    if not args.once and not MONITOR_ALLOWED:
        raise SystemExit('Confirm VOX permits automated monitoring, then set VOX_MONITOR_ALLOWED=true in .env.')
    print('VOX ALERT BOT V3 STARTED')
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
                return
            failures += 1
        except Exception as error:
            print(f'Check failed: {type(error).__name__}')
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
