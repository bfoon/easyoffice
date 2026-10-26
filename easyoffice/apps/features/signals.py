from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from apps.features.models import FeatureFlag


def _bust(**kwargs):
    from apps.features.services import invalidate_cache
    invalidate_cache()


@receiver(post_save, sender=FeatureFlag)
def _flag_saved(sender, **kwargs):
    _bust()


@receiver(post_delete, sender=FeatureFlag)
def _flag_deleted(sender, **kwargs):
    _bust()


@receiver(m2m_changed, sender=FeatureFlag.pilot_users.through)
def _pilot_users_changed(sender, **kwargs):
    _bust()


@receiver(m2m_changed, sender=FeatureFlag.pilot_groups.through)
def _pilot_groups_changed(sender, **kwargs):
    _bust()
