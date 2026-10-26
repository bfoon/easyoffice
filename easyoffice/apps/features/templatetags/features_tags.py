from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def feature_on(context, key):
    """{% feature_on 'pos' as pos_on %}{% if pos_on %}...{% endif %}"""
    from apps.features.services import is_enabled
    request = context.get('request')
    return is_enabled(key, getattr(request, 'user', None))
