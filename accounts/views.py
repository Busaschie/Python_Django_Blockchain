from django.contrib import messages
from django.contrib.auth import get_user_model, login, update_session_auth_hash
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import PasswordChangeForm
from django.shortcuts import redirect, render

from .forms import RegisterForm

User = get_user_model()


@login_not_required
def register(request):
    if request.user.is_authenticated:
        return redirect("index")
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.save(), backend="django.contrib.auth.backends.ModelBackend")
        return redirect("index")
    return render(request, "accounts/register.html", {"form": form})


def account(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)   # angemeldet bleiben
        messages.success(request, "Passwort geändert.")
        return redirect("account")
    return render(request, "accounts/account.html", {"form": form})
