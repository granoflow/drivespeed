"""Offline product evidence; never changes measurement or submission identity."""
import copy
from datetime import date
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata
from urllib.parse import urlsplit, unquote_plus, urlunsplit

MAX_PROFILE_BYTES = 1_048_576
SCHEMA = json.loads((Path(__file__).resolve().parents[1] / 'assets/community/product-profile.schema.json').read_text())


def model_key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).lower().split())


def nominal_capacity(model):
    values = {int(float(n) * (1_000_000_000 if u.lower() == 'gb' else 1_000_000_000_000))
              for n, u in re.findall(r'(?<![\d.])(\d+(?:\.\d+)?)\s*(GB|TB)\b', model, re.I)}
    return next(iter(values)) if len(values) == 1 else None


def canonical_url(value):
    """Conservative lexical identity only; no DNS, fetch or canonical inference."""
    if re.search(r'[\x00-\x20\x7f\\]', value):
        raise ValueError('invalid_profile')
    u = urlsplit(value)
    host = u.hostname or ''
    if u.scheme != 'https' or u.username is not None or u.password is not None or u.port not in (None, 443):
        raise ValueError('invalid_profile')
    if (not re.fullmatch(r'[a-zA-Z0-9.-]+', host) or '.' not in host or host.endswith(('.local', '.internal', '.localhost', '.test'))
            or not re.search(r'[a-zA-Z]', host.rsplit('.', 1)[-1])):
        raise ValueError('invalid_profile')
    def retain_query_part(part):
        raw_key = part.split('=', 1)[0]
        # Match the website's decodeURIComponent contract. urllib otherwise
        # keeps malformed escapes and replaces invalid UTF-8 silently.
        if re.search(r'%(?![0-9a-fA-F]{2})', raw_key):
            raise ValueError('invalid_profile')
        try:
            key = unquote_plus(raw_key, encoding='utf-8', errors='strict').lower()
        except UnicodeError:
            raise ValueError('invalid_profile') from None
        return not key.startswith('utm_') and key not in ('fbclid', 'gclid')
    query = '&'.join(p for p in u.query.split('&') if retain_query_part(p))
    fragment = u.fragment if u.fragment.startswith(('/', '!/')) else ''
    return urlunsplit(('https', ('[' + host.lower() + ']') if ':' in host else host.lower(), u.path or '/', query, fragment))


def validate_profile(value):
    from community_data import _schema, CommunityError
    try:
        _schema(value, SCHEMA)
        def plain(item):
            if isinstance(item, str) and (not item.strip() or any(unicodedata.category(c) in ('Cc', 'Cs') for c in item)):
                raise ValueError('invalid_profile')
            if isinstance(item, dict):
                for child in item.values():
                    plain(child)
            elif isinstance(item, list):
                for child in item:
                    plain(child)
        plain(value)
        if len(set(value['raw_model_aliases'])) != len(value['raw_model_aliases']):
            raise ValueError('invalid_profile')
        for alias in [value['raw_model'], *value['raw_model_aliases']]:
            capacity = nominal_capacity(alias)
            if capacity is not None and capacity != value['capacity_bytes']:
                raise ValueError('identity_conflict')
        if value['name_basis'] == 'official' and not value['official_name']:
            raise ValueError('invalid_profile')
        seen = set()
        for category in ('official_sources', 'review_sources'):
            for item in value[category]:
                if len(set(item['url_aliases'])) != len(item['url_aliases']):
                    raise ValueError('invalid_profile')
                if (item['kind'] == 'review') != (category == 'review_sources'):
                    raise ValueError('invalid_profile')
                if date.fromisoformat(item['checked_on']).isoformat() != item['checked_on']:
                    raise ValueError('invalid_profile')
                if (item['name_method'] == 'title_fallback') != (item['model'] is None and item['provider'] is None):
                    raise ValueError('invalid_profile')
                if item['name_method'] == 'model' and (not item['model'] or not item['provider']):
                    raise ValueError('invalid_profile')
                urls = {canonical_url(u) for u in [item['url'], item['canonical_url'], *item['url_aliases']]}
                if item['identity_basis'] == 'normalized_url' and len(urls) != 1:
                    raise ValueError('identity_conflict')
                if seen.intersection(urls):
                    raise ValueError('identity_conflict')
                seen.update(urls)
    except (CommunityError, KeyError, TypeError, OverflowError) as error:
        raise ValueError('invalid_profile') from error
    return copy.deepcopy(value)


def load_profile(path):
    from community_data import parse_json, CommunityError
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        with os.fdopen(os.open(path, flags), 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PROFILE_BYTES:
                raise ValueError('invalid_profile')
            value = parse_json(source.read(MAX_PROFILE_BYTES + 1), limit=MAX_PROFILE_BYTES)
        return validate_profile(value)
    except CommunityError as error:
        raise ValueError('invalid_profile') from error


def matches_profile(profile, hardware):
    drive = hardware.get('drive', {})
    aliases = {model_key(v) for v in [profile['raw_model'], *profile['raw_model_aliases']]}
    model = drive.get('matching_model') or drive.get('model')
    if not isinstance(model, str) or model_key(model) not in aliases:
        return False
    capacity = drive.get('nominal_capacity_bytes')
    if type(capacity) is not int:
        capacity = nominal_capacity(model)
    return capacity == profile['capacity_bytes']


def save_snapshot(profile, destination):
    """Atomic sidecar replacement; explicit bad input is checked before rendering."""
    destination = Path(destination)
    if any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ValueError('output_failed')
    fd, temporary = tempfile.mkstemp(prefix='.product-profile-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(profile, output, ensure_ascii=False, allow_nan=False, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
