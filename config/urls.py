from django.contrib import admin
from django.urls import include, path

from accounts.views import admin_login
from config.i18n import set_language

urlpatterns = [
    path("sprache/", set_language, name="set_language"),
    path("admin/login/", admin_login, name="admin_login"),   # vor admin.site.urls: mit Drosselung
    path("admin/", admin.site.urls),
    path("", include("accounts.urls")),
    path("", include("backtester.urls")),
]
