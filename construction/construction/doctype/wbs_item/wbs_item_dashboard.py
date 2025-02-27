from frappe import _

def get_data():
    return {
        'fieldname': 'custom_wbs',
        'transactions': [
            {
                'label': _('Material Requests'),
                'items': ['Material Request']
            },
            {
                'label': _('Purchase Orders'),
                'items': ['Purchase Order']
            },
            {
                'label': _('Stock Entries'),
                'items': ['Stock Entry']
            }
        ]
    }
