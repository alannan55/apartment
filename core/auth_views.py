from pathlib import Path

from django.conf import settings
from django.contrib.auth.views import LoginView, LogoutView
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import login_not_required
from django.db import DatabaseError, connection
from django.http import FileResponse, Http404, HttpResponse
from django.views.decorators.cache import never_cache

from .accounts import AccountFileError, load_accounts


class AccountAuthenticationForm(AuthenticationForm):
    error_messages = {
        "invalid_login": "账号或密码不正确。",
        "inactive": "账号已停用。",
    }


class ApartmentLoginView(LoginView):
    template_name = "registration/login.html"
    authentication_form = AccountAuthenticationForm
    redirect_authenticated_user = True


class ApartmentLogoutView(LogoutView):
    pass


@never_cache
def protected_media(request, path):
    root = Path(settings.MEDIA_ROOT).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise Http404
    return FileResponse(target.open("rb"))


@login_not_required
@never_cache
def health(request):
    try:
        if not any(account["is_active"] for account in load_accounts().values()):
            return HttpResponse(status=503)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except (AccountFileError, DatabaseError):
        return HttpResponse(status=503)
    return HttpResponse(status=204)
