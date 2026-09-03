from django.urls import include, path

urlpatterns = [
    path("", include("ip_plan.urls")),
]
