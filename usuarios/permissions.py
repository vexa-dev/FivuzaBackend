"""Permisos DRF específicos de usuarios (RBAC/RRHH), además de los compartidos.

HasModulePermission vive aquí porque delega en PermissionService (services.py),
el único punto de verdad para combinar el permiso heredado del rol con los
overrides individuales de UserPermission.
"""
