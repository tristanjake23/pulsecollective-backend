import unittest
from datetime import date, timedelta

from pydantic import ValidationError
from app.models import Patient, PatientAccount


class PatientValidationTests(unittest.TestCase):
    def test_birth_dates_are_validated_for_both_patient_routes(self):
        for model in (Patient, PatientAccount):
            for value in ("0300-01-01", "2025-02-30", (date.today() + timedelta(days=1)).isoformat()):
                with self.subTest(model=model.__name__, value=value):
                    with self.assertRaises(ValidationError):
                        model(full_name="Test Patient", date_of_birth=value)

    def test_newborn_and_leap_day_are_accepted(self):
        for model in (Patient, PatientAccount):
            self.assertEqual(model(full_name="Test Patient", date_of_birth=date.today()).date_of_birth, date.today())
            self.assertEqual(model(full_name="Test Patient", date_of_birth="2000-02-29").date_of_birth, date(2000, 2, 29))

    def test_invalid_phones_are_rejected_for_both_patient_routes(self):
        for model in (Patient, PatientAccount):
            for value in ("hello", "123", "1234567890123456", "+63abc9123456789"):
                with self.subTest(model=model.__name__, value=value):
                    with self.assertRaises(ValidationError):
                        model(full_name="Test Patient", phone=value)

    def test_optional_contacts_and_international_format(self):
        for model in (Patient, PatientAccount):
            self.assertIsNone(model(full_name="Test Patient", phone="  ").phone)
            self.assertEqual(model(full_name="Test Patient", phone=" +63 912 345 6789 ").phone, "+63 912 345 6789")
        with self.assertRaises(ValidationError):
            Patient(full_name="Test Patient", emergency_contact_phone="abc")


if __name__ == "__main__":
    unittest.main()
