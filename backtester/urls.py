from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="index"),
    path("run/<int:pk>/", views.dashboard, name="detail"),
    path("vergleich/<str:batch>/", views.dashboard, name="compare"),
    path("loeschen/<int:pk>/", views.delete_run, name="delete"),
    path("kommentar/<int:pk>/", views.ai_comment, name="ai_comment"),
    path("status/", views.status, name="status"),
]
