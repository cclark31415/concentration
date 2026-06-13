import os
import secrets

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from auth import enabled_providers, init_oauth
import storage


app = Flask(__name__)
# Trust Azure App Service's reverse-proxy headers so url_for(_external=True)
# uses https (matches the redirect URI registered with OAuth providers).
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") != "development"

oauth = init_oauth(app)


def _current_email() -> str | None:
    return session.get("email")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/privacy")
def privacy():
    return send_file(os.path.join(os.path.dirname(__file__), "legal", "privacy.html"), mimetype="text/html")


@app.route("/terms")
def terms():
    return send_file(os.path.join(os.path.dirname(__file__), "legal", "terms.html"), mimetype="text/html")


@app.route("/health")
def health():
    return {"status": "healthy"}, 200


# --- auth ---

@app.route("/login/<provider>")
def login(provider: str):
    if provider not in enabled_providers():
        return redirect(url_for("index"))
    redirect_uri = url_for("oauth_callback", provider=provider, _external=True)
    client = oauth.create_client(provider)
    return client.authorize_redirect(redirect_uri)


@app.route("/oauth/callback/<provider>")
def oauth_callback(provider: str):
    if provider not in enabled_providers():
        return redirect(url_for("index"))
    client = oauth.create_client(provider)
    try:
        token = client.authorize_access_token()
    except Exception as e:
        app.logger.warning("oauth callback failed for %s: %s", provider, e)
        return redirect(url_for("index", auth_error="callback_failed"))
    info = token.get("userinfo") or client.userinfo()
    email = info.get("email")
    email_verified = info.get("email_verified", True)
    if not email or not email_verified:
        return redirect(url_for("index", auth_error="unverified"))
    subject = info.get("sub") or ""
    display_name = info.get("name") or email.split("@")[0]

    try:
        storage.ensure_tables()
    except Exception as e:
        app.logger.warning("ensure_tables failed: %s", e)

    user, is_new = storage.upsert_user_login(email, display_name, provider, subject)
    session["email"] = user.email
    session["display_name"] = user.display_name
    session["just_signed_in"] = True
    session["is_new_user"] = is_new
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


# --- API ---

@app.route("/api/me")
def api_me():
    email = _current_email()
    if not email:
        return jsonify({
            "authenticated": False,
            "providers": enabled_providers(),
        })
    just_signed_in = bool(session.pop("just_signed_in", False))
    is_new_user = bool(session.pop("is_new_user", False))
    return jsonify({
        "authenticated": True,
        "email": email,
        "display_name": session.get("display_name") or email.split("@")[0],
        "just_signed_in": just_signed_in,
        "is_new_user": is_new_user,
        "providers": enabled_providers(),
    })


@app.route("/api/games", methods=["POST"])
def api_record_game():
    email = _current_email()
    if not email:
        return jsonify({"error": "not_authenticated"}), 401
    data = request.get_json(silent=True) or {}
    try:
        storage.record_game(
            email,
            level=data.get("level"),
            moves=int(data.get("moves", 0)),
            duration_ms=int(data.get("duration_ms", 0)),
            completed_at=data.get("completed_at"),
            client_version=data.get("client_version", ""),
        )
    except (ValueError, TypeError, KeyError) as e:
        return jsonify({"error": "invalid_game", "detail": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/games")
def api_list_games():
    email = _current_email()
    if not email:
        return jsonify({"error": "not_authenticated"}), 401
    try:
        limit = min(max(int(request.args.get("limit", 20)), 1), 100)
    except ValueError:
        limit = 20
    return jsonify({"games": storage.list_games(email, limit=limit)})


@app.route("/api/stats")
def api_stats():
    email = _current_email()
    if not email:
        return jsonify({"error": "not_authenticated"}), 401
    return jsonify({"stats": storage.compute_stats(email)})


@app.route("/api/games/import", methods=["POST"])
def api_import_games():
    email = _current_email()
    if not email:
        return jsonify({"error": "not_authenticated"}), 401
    data = request.get_json(silent=True) or {}
    games = data.get("games") or []
    if not isinstance(games, list):
        return jsonify({"error": "invalid_payload"}), 400
    imported = storage.import_guest_games(email, games)
    return jsonify({"ok": True, "imported": imported})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
