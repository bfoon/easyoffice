from apps.features.registry import FeatureSpec, register

register(FeatureSpec(
    key='investments',
    name='Investments',
    description='Investor invitations, KYC approval, wallets, listings and profit sharing on item sales.',
    icon='bi-graph-up-arrow',
    category='Finance & sales',
    url_prefixes=('/investments/', '/invest/'),
    requires=('inventory',),
    standalone_prefixes=('/invest/',),
    order=50,
))
