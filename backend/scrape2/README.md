# vcasino API scraper (Python)

## Tumhare JS snippet ka matlab

Jo code tumne bheja (`window._cf_chl_opt`, `RpnZR8`, `/cdn-cgi/challenge-platform/.../jsd/`) — **yeh Cloudflare ka bot-protection challenge hai**, site ka casino API decrypt logic **nahi**.

| Cheez | Kya karti hai |
|--------|----------------|
| `_cf_chl_opt`, LZW `Z.h` / `Z.j` | Browser fingerprint compress karke Cloudflare ko bhejna |
| `POST .../jsd/oneshot/...` | Challenge pass karne ke liye |
| API `{"data":"U2FsdGVkX1..."}` | **Alag layer** — app ka CryptoJS AES (passphrase chahiye) |

Is JS se **decrypt password nahi milta**.

## Encrypted response samajhna

```json
{ "data": "U2FsdGVkX1+05z7Ux9Hi+5qhxEpheirJAu98bJmPQl..." }
```

- `U2FsdGVkX1` = Base64 for **`Salted__`** (CryptoJS / OpenSSL format)
- Server JSON ko AES se encrypt karke `data` mein bhejta hai
- Browser mein koi `CryptoJS.AES.decrypt(data, "SECRET")` jaisa code hota hai — **woh secret** tumhe DevTools se nikalna hai

### Passphrase (already extracted)

Site ke `page.html` obfuscated block se key nikal li gayi:

```
cae7b808-8b1e-4f47-87a5-1a4b6a08030e
```

`scrape_vcasino.py` isko default use karta hai (`ENC_RESPONSE` on — POST body bhi encrypt hoti hai).

## Install & run

```bash
cd /Users/abhishekojha/Downloads/scrape
pip install -r requirements.txt
```

### Sirf decrypt (tumhara saved JSON)

`response.json` mein poora API response save karo, phir:

```bash
python scrape_vcasino.py --decrypt-only sample_response.json -o discovered/decrypted_sample.json
```

### Live scrape

Login ke baad browser se cookies export karo (`cookies.txt`):

```bash
python scrape_vcasino.py --cookies cookies.txt --client curl_cffi -o decrypted.json
```

Bina login: API `401 Please Login` deta hai (encrypt/decrypt sahi kaam karta hai).

Endpoints: `--api vcasino` (default) ya `--api casino`.

## Files

- `decrypt_cryptojs.py` — CryptoJS-compatible AES decrypt
- `scrape_vcasino.py` — POST `data2?gtype=vtrio` + decrypt
- `requirements.txt` — requests, beautifulsoup4, pycryptodome, cloudscraper, curl_cffi
- `discovered/decrypted_sample.json` — tumhare `sample_response.json` ka decrypted output
