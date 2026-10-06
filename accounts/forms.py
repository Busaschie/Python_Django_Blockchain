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


class _PasswordConfirm(forms.Form):
    current_password = forms.CharField(label="Aktuelles Passwort", widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))

    def __init__(self, user, *a, **kw):
        super().__init__(*a, **kw)
        self.user = user

    def clean_current_password(self):
        pw = self.cleaned_data["current_password"]
        if not self.user.check_password(pw):
            raise forms.ValidationError("Passwort ist falsch.")
        return pw


class EmailChangeForm(_PasswordConfirm):
    new_email = forms.EmailField(label="Neue E-Mail")
    field_order = ["new_email", "current_password"]

    def clean_new_email(self):
        e = self.cleaned_data["new_email"].strip().lower()
        if e == self.user.username.lower():
            raise forms.ValidationError("Das ist bereits deine E-Mail-Adresse.")
        if User.objects.filter(username__iexact=e).exists():
            raise forms.ValidationError("Diese E-Mail-Adresse ist bereits vergeben.")
        return e


class DeleteAccountForm(_PasswordConfirm):
    confirm = forms.BooleanField(label="Ich weiß, dass mein Konto und alle meine Auswertungen endgültig gelöscht werden.",
                                 error_messages={"required": "Bitte bestätigen."})
