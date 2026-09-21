"""Señales de ventas.

post_save de usuarios.User: libera las cajas asignadas a alguien que quedó
inactivo o dado de baja (Bloque A.2). Sin esto la caja queda a nombre de un
fantasma -User es baja lógica, así que el SET_NULL de la FK nunca se
dispara- y nadie más puede abrirla hasta que alguien lo note.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from usuarios.models import User


@receiver(post_save, sender=User, dispatch_uid="ventas_release_cash_registers")
def release_cash_registers_of_inactive_user(sender, instance, **kwargs):
    # Se corta antes de tocar la base: post_save de User se dispara en cada
    # login (last_login) y la enorme mayoria de esos saves son de gente
    # activa.
    if instance.is_active and instance.deleted_at is None:
        return

    from ventas.models import CashRegister

    CashRegister.objects.filter(assigned_user=instance).update(assigned_user=None)
