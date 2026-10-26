def features(request):
    """
    {{ features.<key> }} → True/False for the current user.
    Unknown keys are True. Also exposes the preview banner spec.
    """
    try:
        from apps.features.services import feature_map
        user = getattr(request, 'user', None)
        return {
            'features': feature_map(user),
            'feature_preview': getattr(request, 'feature_preview', None),
        }
    except Exception:
        from apps.features.services import FeatureMap
        return {'features': FeatureMap(), 'feature_preview': None}
