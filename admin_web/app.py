"""Application assembly which preserves existing user routes without editing main.py."""
import os
from pathlib import Path

from flask import Flask
from flask_cors import CORS

import main as legacy
from admin_web.routes import bp
from admin_web.sessions import initialize

ROOT = Path(__file__).resolve().parents[1]


def create_app(config=None):
    app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
    CORS(app, resources={r"/calculate_bmi": {}, r"/recommend": {}, r"/get_visualizations_data": {}})
    app.config.update(
        SECRET_KEY=os.environ.get("ADMIN_WEB_SECRET"),
        ADMIN_AUTH_DB=os.environ.get("ADMIN_AUTH_DB"),
        ADMIN_WEB_SESSION_DB=os.environ.get("ADMIN_WEB_SESSION_DB"),
        ADMIN_STAGING_DB=os.environ.get("ADMIN_STAGING_DB"),
        ADMIN_REVIEW_DB=os.environ.get("ADMIN_REVIEW_DB"),
        ADMIN_COOKIE_SECURE=True,
    )
    if config:
        app.config.update(config)
    if not app.config.get("SECRET_KEY") or not app.config.get("ADMIN_AUTH_DB") or not app.config.get("ADMIN_WEB_SESSION_DB"):
        raise RuntimeError("Admin secret, authentication database and web session database are required")
    if not app.config.get("TESTING") and not app.config["ADMIN_COOKIE_SECURE"]:
        raise RuntimeError("Production administrator cookies must require HTTPS")
    initialize(app.config["ADMIN_WEB_SESSION_DB"])
    for rule in legacy.app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        app.add_url_rule(rule.rule, endpoint="legacy_" + rule.endpoint,
                         view_func=legacy.app.view_functions[rule.endpoint], methods=rule.methods)
    app.register_blueprint(bp)
    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
