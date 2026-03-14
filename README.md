# x-frame-mirror

A Python reverse-proxy that mirrors **https://www.cubeskills.com/** with two
key modifications:

1. **Removes `X-Frame-Options`** (and `Content-Security-Policy`) headers so
   the site can be embedded inside an `<iframe>`.
2. **Locally caches** mirrored responses to disk (default limit: **512 MB**).

---

## Requirements

* Python 3.10+
* The packages listed in `requirements.txt`

---

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the proxy (default: http://0.0.0.0:5000)
python app.py
```

Then open **http://localhost:5000/** in a browser, or embed it in an iframe:

```html
<iframe src="http://localhost:5000/" width="1280" height="800"></iframe>
```

---

## Configuration

All options are set via environment variables:

| Variable          | Default    | Description                             |
|-------------------|------------|-----------------------------------------|
| `CACHE_DIR`       | `./cache`  | Directory used for the local disk cache |
| `CACHE_SIZE_MB`   | `512`      | Maximum cache size in megabytes         |
| `PORT`            | `5000`     | TCP port the proxy listens on           |
| `HOST`            | `0.0.0.0`  | Bind address                            |
| `REQUEST_TIMEOUT` | `30`       | Upstream request timeout in seconds     |

Example — run on port 8080 with a 256 MB cache:

```bash
PORT=8080 CACHE_SIZE_MB=256 python app.py
```

---

## Production deployment

The built-in Flask development server is single-threaded and not hardened for
production traffic.  For a production deployment use a proper WSGI server such
as [Gunicorn](https://gunicorn.org/):

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

---

## How it works

```
Browser / iframe
      │  GET /path
      ▼
  app.py (Flask)
      │
      ├─ Cache HIT? ──► return cached response (no upstream request)
      │
      └─ Cache MISS
            │  GET https://www.cubeskills.com/path
            ▼
      cubeskills.com
            │  response
            ▼
      Strip security headers:
        • X-Frame-Options
        • Content-Security-Policy (all variants)
        • Strict-Transport-Security
      Rewrite URLs in HTML / CSS / JS → proxy-relative paths
      Store in disk cache (LRU eviction at 512 MB)
            │
            ▼
      Browser / iframe
```

### Headers removed

| Header                              | Why |
|-------------------------------------|-----|
| `X-Frame-Options`                   | Blocks iframe embedding |
| `Content-Security-Policy`           | Can block iframe / mixed-content |
| `X-Content-Security-Policy`         | Legacy CSP header |
| `Content-Security-Policy-Report-Only` | Informational but unnecessary |
| `Strict-Transport-Security`         | Not meaningful through a proxy |

### URL rewriting

* **HTML** – `href`, `src`, `action`, `data`, `srcset` attributes and
  `<meta http-equiv="X-Frame-Options|Content-Security-Policy">` tags are
  rewritten / removed via BeautifulSoup.
* **CSS** – `url(…)` expressions are rewritten with a regex substitution.
* **JavaScript** – string literals containing the target origin are replaced
  with proxy-relative paths.

### Cache

Uses [diskcache](https://grantjenks.com/docs/diskcache/) for thread-safe,
size-bounded on-disk storage.  Only `GET` requests with a `200 OK` status are
cached.  Entries are evicted in least-recently-used order once the size limit
is reached.
