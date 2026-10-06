from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.urls import path, reverse_lazy

ln = login_not_required

from . import views
from .forms import LoginForm

urlpatterns = [
    path("anmelden/", login_not_required(auth_views.LoginView.as_view(
        template_name="accounts/login.html", authentication_form=LoginForm, redirect_authenticated_user=True)), name="login"),
    path("abmelden/", auth_views.LogoutView.as_view(), name="logout"),
    path("registrieren/", views.register, name="register"),
    path("registrieren/gesendet/", views.register_sent, name="register_sent"),
    path("registrieren/bestaetigen/<token>/", views.register_confirm, name="register_confirm"),
    path("passwort-vergessen/", ln(auth_views.PasswordResetView.as_view(
        template_name="accounts/forgot.html", email_template_name="accounts/reset_email.txt",
        subject_template_name="accounts/reset_subject.txt", success_url=reverse_lazy("forgot_done"))), name="forgot"),
    path("passwort-vergessen/gesendet/", ln(auth_views.PasswordResetDoneView.as_view(
        template_name="accounts/forgot_done.html")), name="forgot_done"),
    path("passwort-zuruecksetzen/<uidb64>/<token>/", ln(auth_views.PasswordResetConfirmView.as_view(
        template_name="accounts/reset_confirm.html", success_url=reverse_lazy("reset_done"))), name="password_reset_confirm"),
    path("passwort-zuruecksetzen/fertig/", ln(auth_views.PasswordResetCompleteView.as_view(
        template_name="accounts/reset_done.html")), name="reset_done"),
    path("konto/", views.account, name="account"),
    path("konto/email/", views.email_change, name="email_change"),
    path("konto/email/bestaetigen/<token>/", views.email_change_confirm, name="email_change_confirm"),
    path("konto/loeschen/", views.account_delete, name="account_delete"),
]
