import hashlib
import ipaddress

from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import SESSION_KEY
from django.core.cache import cache
from django.shortcuts import render


class LoginRateLimitMiddleware:
    """A small per-IP limit for the single-worker deployment, before password hashing."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and request.path in ("/login/", "/admin/login/"):
            address = request.META.get("REMOTE_ADDR", "unknown")
            if settings.APARTMENT_TRUST_PROXY:
                forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
                try:
                    address = str(ipaddress.ip_address(forwarded))
                except ValueError:
                    pass
            key = "login-attempts:" + hashlib.sha256(address.encode()).hexdigest()
            if cache.add(key, 1, timeout=300):
                attempts = 1
            else:
                try:
                    attempts = cache.incr(key)
                except ValueError:
                    cache.set(key, 1, timeout=300)
                    attempts = 1
            if attempts > 10:
                response = render(request, "registration/login.html", {
                    "form": AuthenticationForm(request),
                    "login_error": "登录尝试过于频繁，请在 5 分钟后重试。",
                }, status=429)
                response["Retry-After"] = "300"
                return response
        return self.get_response(request)


class AccountSessionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if SESSION_KEY in request.session and not request.user.is_authenticated:
            request.session.flush()
        response = self.get_response(request)
        if request.user.is_authenticated and "no-store" not in response.get("Cache-Control", ""):
            response["Cache-Control"] = "private, no-store"
        return response
