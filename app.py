"""Promo Finder web app.

Vercel runs this file as one serverless function; the page itself is static,
in public/. Locally it serves both:

    python app.py            then open http://localhost:5000
"""
from __future__ import annotations

import re
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_from_directory

load_dotenv()

from promofinder import web  # noqa: E402  (after .env is loaded)
from promofinder.llm import LLMError  # noqa: E402

PUBLIC = Path(__file__).resolve().parent / "public"

app = Flask(__name__, static_folder=None)
app.json.sort_keys = False
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024


def _error(status: int, message: str):
    # never echo anything that looks like a key back to the page
    message = re.sub(r"(sk|gsk|AIza|sk-ant|sk-or)[-_A-Za-z0-9]{8,}", "[key hidden]", message)
    return jsonify({"error": message[:500]}), status


@app.get("/api/config")
def api_config():
    return jsonify(web.config())


@app.post("/api/<step>")
def api_step(step: str):
    if step not in web.STEPS:
        abort(404)
    fn, needs_model, long_call = web.STEPS[step]
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error(400, "Expected a JSON body.")
    try:
        out = fn(body, web.make_llm(request.headers, long_call)) if needs_model else fn(body)
        return jsonify(out)
    except web.AuthError as e:
        return _error(401, str(e))
    except web.InputError as e:
        return _error(400, str(e))
    except LLMError as e:
        return _error(502, f"The model call failed: {e}")
    except (KeyError, IndexError, TypeError, ValueError) as e:
        return _error(400, f"Bad input ({type(e).__name__}: {e})")


@app.errorhandler(413)
def too_large(_):
    return _error(413, "That upload is too big for one request.")


# The page. On Vercel the CDN serves public/ before requests reach this app;
# these routes make `python app.py` work the same way locally.
@app.get("/")
def index():
    return send_from_directory(PUBLIC, "index.html")


@app.get("/<path:name>")
def static_files(name: str):
    if name.startswith("api/"):
        abort(404)
    return send_from_directory(PUBLIC, name)


if __name__ == "__main__":
    import os

    os.environ["PROMO_LOCAL"] = "1"  # your own machine: answers are cached in .cache/
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
