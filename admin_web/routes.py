"""Flask routes for the authenticated nutrition review console."""
from functools import wraps
import json
from pathlib import Path
import secrets
import sqlite3

from flask import (Blueprint, abort, current_app, flash, make_response, redirect,
                   render_template, request, url_for)
from itsdangerous import BadData, URLSafeTimedSerializer

from admin_web import sessions
from admin_web.services import dashboard, food_detail, list_foods
from nutrition.auth.admin_auth import (AuthenticationError, authenticate,
                                        authorized_review_decision, get_account_state)
from nutrition.auth.rbac import has_permission
from nutrition.review.policy import DEFAULT_POLICY

bp = Blueprint("admin", __name__, url_prefix="/admin")
COOKIE = "admin_session"
LOGIN_CSRF_COOKIE = "admin_login_csrf"


def _auth_path(): return current_app.config["ADMIN_AUTH_DB"]
def _session_path(): return current_app.config["ADMIN_WEB_SESSION_DB"]
def _staging_path(): return current_app.config.get("ADMIN_STAGING_DB")
def _review_path(): return current_app.config.get("ADMIN_REVIEW_DB")


def _state(account_id):
    try:
        return get_account_state(_auth_path(), account_id)
    except (ValueError, OSError, sqlite3.Error):
        return None


def _session(touch=True):
    return sessions.resolve(_session_path(), request.cookies.get(COOKIE), _state, touch=touch)


def protected(permission):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            principal = _session()
            if principal is None:
                return redirect(url_for("admin.login", next=request.path))
            if not has_permission(principal["role"], permission):
                abort(403)
            return view(principal, *args, **kwargs)
        return wrapped
    return decorate


def _login_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="admin-login-csrf-v1")


def _new_login_csrf():
    return _login_serializer().dumps({"nonce": secrets.token_urlsafe(16)})


def _valid_login_csrf(value):
    cookie = request.cookies.get(LOGIN_CSRF_COOKIE)
    if not value or not cookie or not secrets.compare_digest(value, cookie):
        return False
    try:
        _login_serializer().loads(value, max_age=600)
        return True
    except BadData:
        return False


def _safe_next(value):
    return value if isinstance(value, str) and value.startswith("/admin/") and not value.startswith("//") else url_for("admin.dashboard_view")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if not _valid_login_csrf(request.form.get("csrf_token")):
            token = _new_login_csrf()
            response = make_response(render_template("admin/login.html", csrf_token=token, error="请求验证失败"), 400)
            response.set_cookie(LOGIN_CSRF_COOKIE, token, httponly=True,
                                secure=current_app.config["ADMIN_COOKIE_SECURE"], samesite="Strict",
                                max_age=600, path="/admin")
            return response
        try:
            principal = authenticate(_auth_path(), request.form.get("username", ""), request.form.get("password", ""))
            state = get_account_state(_auth_path(), principal.account_id)
            token, csrf = sessions.create(_session_path(), principal.account_id, state["auth_version"])
        except (AuthenticationError, ValueError, OSError, sqlite3.Error):
            return render_template("admin/login.html", csrf_token=request.form.get("csrf_token"),
                                   error="用户名或密码错误，或账户不可用"), 401
        response = redirect(_safe_next(request.form.get("next")))
        response.set_cookie(COOKIE, token, httponly=True, secure=current_app.config["ADMIN_COOKIE_SECURE"],
                            samesite="Strict", max_age=8 * 60 * 60, path="/admin")
        response.set_cookie("admin_csrf", csrf, httponly=False, secure=current_app.config["ADMIN_COOKIE_SECURE"],
                            samesite="Strict", max_age=8 * 60 * 60, path="/admin")
        response.delete_cookie(LOGIN_CSRF_COOKIE, path="/admin")
        return response
    token = _new_login_csrf()
    response = make_response(render_template("admin/login.html", csrf_token=token, error=None))
    response.set_cookie(LOGIN_CSRF_COOKIE, token, httponly=True,
                        secure=current_app.config["ADMIN_COOKIE_SECURE"], samesite="Strict",
                        max_age=600, path="/admin")
    return response


def _csrf_or_abort(principal):
    if not sessions.verify_csrf(principal, request.form.get("csrf_token")):
        abort(400, "请求验证失败")


@bp.post("/logout")
@protected("audit.read")
def logout(principal):
    _csrf_or_abort(principal)
    sessions.revoke(_session_path(), request.cookies.get(COOKIE))
    response = redirect(url_for("admin.login"))
    response.delete_cookie(COOKIE, path="/admin")
    response.delete_cookie("admin_csrf", path="/admin")
    return response


@bp.get("/dashboard")
@protected("audit.read")
def dashboard_view(principal):
    model = dashboard(_staging_path(), _review_path())
    return render_template("admin/dashboard.html", principal=principal, model=model,
                           csrf_token=request.cookies.get("admin_csrf", ""))


@bp.get("/foods")
@protected("review.history")
def foods(principal):
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    model = list_foods(_staging_path(), _review_path(), search=request.args.get("q", ""),
                       release=request.args.get("release", ""), decision=request.args.get("decision", ""),
                       evaluation=request.args.get("evaluation", ""), page=page)
    return render_template("admin/foods.html", principal=principal, model=model,
                           query=request.args.get("q", ""), csrf_token=request.cookies.get("admin_csrf", ""))


@bp.get("/foods/<release_key>/<int:fdc_id>")
@protected("review.history")
def detail(principal, release_key, fdc_id):
    if len(release_key) > 128 or not _staging_path() or not Path(_staging_path()).is_file():
        abort(404)
    try:
        model = food_detail(_staging_path(), _review_path(), release_key, fdc_id)
    except (ValueError, sqlite3.Error, json.JSONDecodeError):
        abort(503)
    if model is None:
        abort(404)
    return render_template("admin/food_detail.html", principal=principal, model=model,
                           csrf_token=request.cookies.get("admin_csrf", ""),
                           operation_id="web-" + secrets.token_urlsafe(24))


@bp.post("/reviews/decide")
@protected("review.decide")
def decide(principal):
    _csrf_or_abort(principal)
    if request.form.get("reviewer_id") or request.form.get("identity_assurance"):
        abort(400, "禁止提交身份字段")
    try:
        account = authenticate(_auth_path(), principal["username"], request.form.get("current_password", ""))
        if account.account_id != principal["account_id"]:
            raise PermissionError("Account mismatch")
        record_sha = request.form.get("record_sha256", "")
        release_key, fdc_id = request.form.get("release_key", ""), int(request.form.get("fdc_id", "0"))
        detail_model = food_detail(_staging_path(), _review_path(), release_key, fdc_id)
        if not detail_model or not secrets.compare_digest(record_sha, detail_model["record_sha256"]):
            raise ValueError("食品记录已变化，请刷新后重试")
        resolutions = {}
        for key in detail_model["evaluation"]["required_issue_resolution_keys"]:
            value = request.form.get("resolution_" + key, "").strip()
            if value:
                resolutions[key] = value
        result = authorized_review_decision(
            _auth_path(), account, _review_path(), _staging_path(), release_key=release_key,
            fdc_id=fdc_id, operation_id=request.form.get("operation_id", ""),
            event_type=request.form.get("decision", ""), replacement_decision=request.form.get("replacement_decision") or None,
            review_comment=request.form.get("comment", ""), review_reason=request.form.get("reason", ""),
            issue_resolutions=resolutions, evidence_references=[], policy=DEFAULT_POLICY,
            provenance_evidence={})
        if result.get("formal_approval_created"):
            raise RuntimeError("Formal approval is forbidden")
    except (AuthenticationError, PermissionError):
        return render_template("admin/error.html", message="身份验证失败"), 403
    except (ValueError, TypeError, OSError, sqlite3.Error):
        return render_template("admin/error.html", message="审核操作失败，请检查输入或数据源状态"), 400
    flash("审核决定已记录；该操作不会创建正式 approved 食品。")
    return redirect(url_for("admin.detail", release_key=release_key, fdc_id=fdc_id))


@bp.after_app_request
def secure_admin_response(response):
    if request.path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'"
    return response
