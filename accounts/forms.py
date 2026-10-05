from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import AuthenticationForm

User = get_user_model()


class LoginForm(AuthenticationForm):
    username = forms.EmailField(label="E-Mail", widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}))

    def clean_username(self):
        return self.cleaned_data["username"].strip().lower()


class RegisterForm(forms.Form):
    email = forms.EmailField(label="E-Mail (= Benutzername)")
    password1 = forms.CharField(label="Passwort", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password2 = forms.CharField(label="Passwort wiederholen", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(username__iexact=email).exists():
            raise forms.ValidationError("Zu dieser E-Mail existiert bereits ein Konto.")
        return email

    def clean(self):
        data = super().clean()
        p1, p2 = data.get("password1"), data.get("password2")
        if p1 and p2:
            if p1 != p2:
                self.add_error("password2", "Die Passwörter stimmen nicht überein.")
            else:
                try:
                    password_validation.validate_password(p1, User(username=data.get("email", "")))
                except forms.ValidationError as e:
                    self.add_error("password1", e)
        return data

    def save(self):
        e = self.cleaned_data["email"]
        return User.objects.create_user(username=e, email=e, password=self.cleaned_data["password1"])
