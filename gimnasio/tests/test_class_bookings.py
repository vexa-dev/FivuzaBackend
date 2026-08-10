# Pruebas de ClassBookingService: reserva de cupo, asistencia y cancelacion.
from datetime import date

from django_tenants.test.cases import TenantTestCase

from core.models import TenantSettings
from gimnasio.models import ClassBooking, GymClass
from gimnasio.services import (
    ClassBookingAlreadyCancelledError,
    ClassBookingNotReservedError,
    ClassBookingService,
    ClassFullError,
)
from inventario.models import Warehouse
from usuarios.models import Employee, Role, User
from ventas.models import Customer


class ClassBookingServiceTests(TenantTestCase):
    """ClassBookingService (Sprint 30, Ficha de Producto §5.1): reservar
    cupo respetando GymClass.max_capacity, marcar asistencia y cancelar."""

    @classmethod
    def get_test_schema_name(cls):
        return "test_gimnasio_class_booking_service"

    @classmethod
    def get_test_tenant_domain(cls):
        return "test-gimnasio-class-booking-service.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        role = Role.objects.get(name="admin")
        cls.user = User.objects.create(email="admin@negocio.com", role=role)
        cls.warehouse = Warehouse.objects.create(name="Principal")
        cls.instructor = Employee.objects.create(
            full_name="Carla Rojas",
            document_number="77778888",
            position="Instructora",
            warehouse=cls.warehouse,
            salary_type="MONTHLY",
            salary_amount="1800.00",
            hire_date=date(2026, 1, 1),
        )
        cls.gym_class = GymClass.objects.create(
            name="Spinning",
            instructor=cls.instructor,
            max_capacity=2,
            duration_minutes=45,
        )

    @classmethod
    def tearDownClass(cls):
        TenantSettings.objects.filter(tenant=cls.tenant).delete()
        super().tearDownClass()

    def _customer(self, document_number):
        return Customer.objects.create(
            document_type="DNI", document_number=document_number, name="Socio"
        )

    def test_book_class_within_capacity_succeeds(self):
        customer = self._customer("11111111")
        booking = ClassBookingService.book_class(
            customer=customer, gym_class=self.gym_class, class_date=date(2026, 9, 1)
        )
        self.assertEqual(booking.status, "RESERVADO")

    def test_book_class_at_full_capacity_raises(self):
        ClassBookingService.book_class(
            customer=self._customer("22222222"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 2),
        )
        ClassBookingService.book_class(
            customer=self._customer("33333333"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 2),
        )
        with self.assertRaises(ClassFullError):
            ClassBookingService.book_class(
                customer=self._customer("44444444"),
                gym_class=self.gym_class,
                class_date=date(2026, 9, 2),
            )

    def test_cancelling_a_booking_frees_up_the_slot(self):
        first = ClassBookingService.book_class(
            customer=self._customer("55555555"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 3),
        )
        ClassBookingService.book_class(
            customer=self._customer("66666666"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 3),
        )
        ClassBookingService.cancel_booking(booking=first)

        # Con un cupo liberado, un tercer socio si puede reservar el mismo dia.
        booking = ClassBookingService.book_class(
            customer=self._customer("77777777"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 3),
        )
        self.assertEqual(booking.status, "RESERVADO")

    def test_different_dates_have_independent_capacity(self):
        ClassBookingService.book_class(
            customer=self._customer("81111111"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 4),
        )
        ClassBookingService.book_class(
            customer=self._customer("82222222"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 4),
        )
        # 9/4 esta lleno, pero 9/5 es una sesion distinta con su propio cupo.
        booking = ClassBookingService.book_class(
            customer=self._customer("83333333"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 5),
        )
        self.assertEqual(booking.status, "RESERVADO")

    def test_cancel_already_cancelled_booking_raises(self):
        booking = ClassBookingService.book_class(
            customer=self._customer("91111111"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 6),
        )
        ClassBookingService.cancel_booking(booking=booking)
        with self.assertRaises(ClassBookingAlreadyCancelledError):
            ClassBookingService.cancel_booking(booking=booking)

    def test_mark_attendance_present_and_absent(self):
        present = ClassBookingService.book_class(
            customer=self._customer("92222222"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 7),
        )
        absent = ClassBookingService.book_class(
            customer=self._customer("93333333"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 7),
        )
        ClassBookingService.mark_attendance(booking=present, attended=True)
        ClassBookingService.mark_attendance(booking=absent, attended=False)

        present.refresh_from_db()
        absent.refresh_from_db()
        self.assertEqual(present.status, "ASISTIO")
        self.assertEqual(absent.status, "NO_ASISTIO")

    def test_mark_attendance_on_non_reserved_booking_raises(self):
        booking = ClassBookingService.book_class(
            customer=self._customer("94444444"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 8),
        )
        ClassBookingService.cancel_booking(booking=booking)
        with self.assertRaises(ClassBookingNotReservedError):
            ClassBookingService.mark_attendance(booking=booking, attended=True)

    def test_attended_booking_still_counts_towards_capacity(self):
        # Marcar asistencia no libera el cupo -sigue contando ASISTIO.
        attended = ClassBookingService.book_class(
            customer=self._customer("95555555"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 9),
        )
        ClassBookingService.mark_attendance(booking=attended, attended=True)
        ClassBookingService.book_class(
            customer=self._customer("96666666"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 9),
        )
        with self.assertRaises(ClassFullError):
            ClassBookingService.book_class(
                customer=self._customer("97777777"),
                gym_class=self.gym_class,
                class_date=date(2026, 9, 9),
            )

    def test_no_show_frees_capacity_for_reporting_but_booking_stays_recorded(self):
        booking = ClassBookingService.book_class(
            customer=self._customer("98888888"),
            gym_class=self.gym_class,
            class_date=date(2026, 9, 10),
        )
        ClassBookingService.mark_attendance(booking=booking, attended=False)
        self.assertEqual(
            ClassBooking.objects.filter(
                gym_class=self.gym_class, class_date=date(2026, 9, 10)
            ).count(),
            1,
        )
