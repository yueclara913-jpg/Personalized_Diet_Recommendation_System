"""Evidence-based numeric unit conversion; never infer a nutrition denominator."""
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_EVEN, Context, DecimalException

VERSION = '1.0.0'
UNITS = {'g': 'g', 'G': 'g', 'mg': 'mg', 'MG': 'mg', 'kcal': 'kcal', 'KCAL': 'kcal', 'kJ': 'kJ', 'KJ': 'kJ'}


def convert_value(raw_value, raw_unit, target_unit, *, basis, basis_evidence):
    result = dict(raw_value=raw_value, raw_unit=raw_unit, standardized_value=None,
                  standardized_unit=target_unit, conversion_rule=None,
                  conversion_version=VERSION, status='needs_review', reason=None)
    if raw_value is None:
        return dict(result, status='missing', reason='missing_value')
    if not isinstance(basis_evidence, str) or not basis_evidence.strip() or basis != 'per_100g':
        return dict(result, reason='unsupported_or_unverified_basis')
    source = UNITS.get(raw_unit) if isinstance(raw_unit, str) else None
    target = UNITS.get(target_unit) if isinstance(target_unit, str) else None
    if source is None or target is None:
        return dict(result, reason='unknown_unit')
    try:
        if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int, float, Decimal)):
            raise ValueError
        number = Decimal(str(raw_value))
        if not number.is_finite():
            return dict(result, reason='non_finite_value')
        if number < 0 or number > Decimal('1e12'):
            return dict(result, reason='out_of_range')
        if len(number.as_tuple().digits) > 100:
            return dict(result, reason='excessive_precision')
        with localcontext(Context(prec=110, rounding=ROUND_HALF_EVEN)) as context:
            context.prec = 110
            context.rounding = ROUND_HALF_EVEN
            factors = {('kJ','kcal'): (Decimal(1)/Decimal('4.184'), 'kJ_to_kcal_div_4.184'),
                       ('kcal','kJ'): (Decimal('4.184'), 'kcal_to_kJ_mul_4.184'),
                       ('g','mg'): (Decimal(1000), 'g_to_mg_mul_1000'),
                       ('mg','g'): (Decimal('0.001'), 'mg_to_g_div_1000')}
            if source == target:
                value, rule = number, 'identity'
            elif (source, target) in factors:
                factor, rule = factors[source, target]
                value = number * factor
            else:
                return dict(result, reason='incompatible_units')
            if value > Decimal('1e12'):
                return dict(result, reason='out_of_range')
            rounded = value.quantize(Decimal('0.000001'))
            if value != 0 and rounded == 0:
                return dict(result, reason='below_output_precision')
            return dict(result, standardized_value=format(rounded, 'f'),
                        conversion_rule=rule, status='converted', reason=None)
    except (DecimalException, ValueError, OverflowError):
        return dict(result, reason='invalid_numeric_value')
