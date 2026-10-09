from django.contrib.auth import logout

from . import demo


class DemoExpiryMiddleware:
    """Abgelaufene Demo-Konten werden abgemeldet (und per Abmelde-Signal gelöscht)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        u = request.user
        if demo.is_demo(u):
            if demo.is_active(u):
                demo.touch(u)
            else:
                logout(request)
        return self.get_response(request)
