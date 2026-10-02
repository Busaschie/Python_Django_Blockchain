from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="index"),
    path("run/<int:pk>/", views.dashboard, name="detail"),
    path("vergleich/<str:batch>/", views.dashboard, name="compare"),
    path("status/", views.status, name="status"),
]
