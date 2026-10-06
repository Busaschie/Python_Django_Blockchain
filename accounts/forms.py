from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import AuthenticationForm

User = get_user_model()


class LoginForm(AuthenticationForm):
    username = forms.EmailField(label="E-Mail", widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}))

    def clean_username(self):
        return self.cleaned_data["username"].strip().lower()


class EmailForm(forms.Form):
    """Schritt 1: nur die E-Mail-Adresse."""
    email = forms.EmailField(label="E-Mail (= Benutzername)")

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class PasswordSetForm(forms.Form):
    """Schritt 2 (nach Bestätigung): Passwort festlegen."""
    password1 = forms.CharField(label="Passwort", widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "autofocus": True}))
    password2 = forms.CharField(label="Passwort wiederholen", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    def __init__(self, *a, email="", **kw):
        super().__init__(*a, **kw)
        self.email = email

    def clean(self):
        data = super().clean()
        p1, p2 = data.get("password1"), data.get("password2")
        if p1 and p2:
            if p1 != p2:
                self.add_error("password2", "Die Passwörter stimmen nicht überein.")
            else:
                try:
                    password_validation.validate_password(p1, User(username=self.email, email=self.email))
                except forms.ValidationError as e:
                    self.add_error("password1", e)
        return data
