from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

User = get_user_model()


class CurrentUserProfileApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="profile@example.com",
            password="test-password-123",
            first_name="Sam",
            last_name="User",
        )
        self.client.force_authenticate(self.user)

    def test_profile_can_update_names(self):
        response = self.client.patch(
            "/api/users/me/",
            {"first_name": "Taylor", "last_name": "Example"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Taylor")
        self.assertEqual(self.user.last_name, "Example")

    def test_profile_cannot_change_email_or_staff_permissions(self):
        response = self.client.patch(
            "/api/users/me/",
            {"email": "attacker@example.com", "is_staff": True, "is_superuser": True},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "profile@example.com")
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
