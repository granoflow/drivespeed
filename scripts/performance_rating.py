"""Evidence-bound throughput ratings; no authenticity or health classification."""
import json
import math
from datetime import date
from pathlib import Path


def positive(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def interface_catalog():
    return json.loads((Path(__file__).resolve().parent.parent / 'assets/interface-reference.json').read_text(encoding='utf-8'))


def link_reference(link):
    """A reported segment bound, not application throughput or device capability."""
    for item in interface_catalog()['items']:
        if item['kind'] != link.get('kind'):
            continue
        if item['kind'] == 'pcie' and item['gt_s'] == link.get('gt_s'):
            lanes = link.get('lanes')
            if not positive(lanes) or int(lanes) != lanes or lanes not in (1, 2, 4, 8, 16, 32):
                return None
            return dict(label=f"PCIe {item['generation']}.0 ×{int(lanes)}", mb_s=item['gt_s'] * lanes * item['encoding'] * 125,
                        source_url=item['source_url'], basis='encoded')
        if item['kind'] in ('usb', 'sata') and item['gbps'] == link.get('gbps'):
            return dict(label=f"{item['kind'].upper()} {item['gbps']} Gbps", mb_s=item.get('nominal_mb_s', item.get('encoded_mb_s')),
                        source_url=item['source_url'], basis='nominal' if item['kind'] == 'usb' else 'encoded')
    return None


def comparison_context(result, case):
    """Only public method/conditions; no paths, serials or measured scores."""
    target, env, plan = (result.get(key, {}) for key in ('target', 'environment', 'plan'))
    hardware = result.get('hardware', {})
    links = hardware.get('links', [])
    if (target.get('scope') != 'physical' or not links or
        any(link.get('state') != 'negotiated' or not link_reference(link) for link in links)):
        return None
    models = {kind: hardware.get(kind, {}).get('model') for kind in ('computer', 'drive', 'enclosure', 'controller')}
    if not models['drive'] or not models['computer']:
        return None
    # A USB bridge must be positively identified; candidates are not identities.
    if any(link.get('kind') == 'usb' for link in links) and not models['controller']:
        return None
    context = dict(method_version=result.get('tool', {}).get('method_version'),
                   os_family=env.get('os_family'), os_version=env.get('os_version'), engine=env.get('engine'),
                   filesystem=target.get('filesystem'), capacity_bytes=target.get('total_bytes'),
                   profile=plan.get('profile'), file_bytes=plan.get('file_bytes'),
                   rw=case.get('rw'), bs=case.get('bs'), qd=case.get('qd'), numjobs=1, direct=True,
                   runtime=case.get('runtime'), io_bytes=case.get('io_bytes'), models=models,
                   links=[{key: link[key] for key in ('kind', 'gt_s', 'lanes', 'gbps') if key in link} for link in links],
                   phase='post_cache_stable' if case.get('name') == 'sustained' else 'short')
    free, total = target.get('free_bytes'), target.get('total_bytes')
    if not positive(total) or not positive(free):
        return None
    context['free_percent_bucket'] = int(100 * free / total) // 10
    if any(value is None or value == '' for value in context.values()) or context['method_version'] != 'drivespeed-file-v1':
        return None
    return context


def performance_ratings(result, today=None):
    """Five ascending source-defined bands; abstain if evidence is not comparable."""
    from reference_data import validate_reference_data
    refs = validate_reference_data(result.get('references') or {'items': []})
    rows = []
    today = today or date.today()
    try:
        fresh = 0 <= (today - date.fromisoformat(refs.get('checked_at', ''))).days <= 30
    except (ValueError, TypeError):
        fresh = False
    hardware = result.get('hardware', {})
    drive = hardware.get('drive', {}).get('model')
    for metric, case_name, direction in [('seq_read', 'seq_read', 'read'), ('seq_write', 'seq_write', 'write'),
                                         ('rand_read', 'rand_read', 'read'), ('rand_write', 'rand_write', 'write'),
                                         ('post_cache_mb_s', 'sustained', 'write')]:
        row = dict(metric=metric, level=None, measured=None, reason='measurement_missing')
        rows.append(row)
        matches = [case for case in result.get('cases', []) if case.get('name') == case_name]
        if len(matches) != 1 or matches[0].get('status') != 'completed' or matches[0].get('metrics_complete') is not True:
            continue
        case = matches[0]
        if metric.startswith('rand_') and (case.get('bs') != 4096 or case.get('qd') != 1 or case.get('rw') != ('randread' if direction == 'read' else 'randwrite')):
            row['reason'] = 'conditions_unknown'
            continue
        value = case.get('metrics', {}).get(direction, {}).get('mb_s')
        if case_name == 'sustained':
            assessment = result.get('cache_assessment', {})
            value = assessment.get('post_cache_mb_s') if assessment.get('status') == 'observed_drop_stable' else None
        if not positive(value):
            continue
        row['measured'] = value
        context = comparison_context(result, case)
        row['reason'] = 'conditions_unknown' if context is None else 'reference_missing'
        if context is None:
            continue
        link_bound = min(link_reference(link)['mb_s'] for link in hardware['links'])
        if value > link_bound:
            row['reason'] = 'above_link_bound'
            continue
        if not fresh:
            row['reason'] = 'reference_stale'
            continue
        eligible = [item for item in refs['items'] if item.get('component') == 'drive' and item.get('metric') == metric
                    and item.get('relationship') == 'device' and item.get('model') == drive == refs.get('for_drive_model')
                    and item.get('comparison') == context and item.get('rating_bands')]
        if len(eligible) != 1:
            row['reason'] = 'reference_ambiguous' if len(eligible) > 1 else 'reference_missing'
            continue
        item = eligible[0]
        bounds = item['rating_bands']['upper_bound_mb_s']
        if value > bounds:
            row['reason'] = 'above_reference_range'
            continue
        thresholds = item['rating_bands']['thresholds_mb_s']
        row.update(level=1 + sum(value >= cutoff for cutoff in thresholds), reason='scored', reference=item,
                   ratio=value / item['value'])
    return rows


def component_capabilities(result):
    """Exact official specifications; never infer SSD speed from its bus label."""
    from reference_data import validate_reference_data
    hardware = result.get('hardware', {})
    refs = validate_reference_data(result.get('references') or {'items': []})
    caps = []
    try:
        checked = date.fromisoformat(refs.get('checked_at', '')).isoformat()
    except (TypeError, ValueError):
        checked = None
    if checked and refs.get('for_drive_model') == hardware.get('drive', {}).get('model'):
        for component in ('controller', 'drive'):
            model = hardware.get(component, {}).get('model')
            for direction in ('read', 'write'):
                candidates = [item for item in refs['items'] if item['component'] == component
                              and model and item['model'] == model and item['relationship'] == 'device'
                              and item['source_kind'] == 'official'
                              and item['metric'] in ('seq_' + direction, 'seq_' + direction + '_q')
                              and not item.get('rating_bands')]
                # Conflicting capacity/revision claims require research, not a best guess.
                if len({item['value'] for item in candidates}) != 1:
                    continue
                item = candidates[0]
                caps.append(dict(component=component, model=model, read_mb_s=item['value'] if direction == 'read' else None,
                                 write_mb_s=item['value'] if direction == 'write' else None, basis='rated',
                                 source_url=item['source_url'], source_label=item['source_label'], checked_on=checked,
                                 conditions=item['conditions']))
    catalog = interface_catalog()
    for chip in catalog['controllers']:
        if chip['model'] == hardware.get('controller', {}).get('model') and not any(c['component'] == 'controller' for c in caps):
            caps.append(dict(component='controller', model=chip['model'], read_mb_s=chip['gbps'] * 125,
                             write_mb_s=chip['gbps'] * 125, basis='signal', source_url=chip['source_url'],
                             source_label='ASMedia', checked_on=catalog['checked_at'], conditions=chip['interface']))
    return caps


def utilisation_summary(measured, capabilities, link_bounds, physical=True):
    """Project display bands, not a health test or throughput guarantee."""
    rows = []
    for component in ('controller', 'interface', 'drive'):
        refs = [c for c in capabilities if c['component'] == component]
        if component == 'interface' and link_bounds:
            refs = [dict(read_mb_s=min(link_bounds), write_mb_s=min(link_bounds), basis='signal')]
        directions = []
        for direction in ('read', 'write'):
            values = {c[direction + '_mb_s'] for c in refs if positive(c.get(direction + '_mb_s'))}
            if physical and len(values) == 1 and positive(measured.get(direction)):
                bound = next(iter(values))
                directions.append(dict(direction=direction, measured=measured[direction], bound=bound, ratio=measured[direction] / bound))
        ratio = sum(d['ratio'] for d in directions) / len(directions) if directions else None
        rows.append(dict(component=component, level=min(5, 1 + sum(ratio >= t for t in (.2, .4, .6, .8))) if ratio is not None else None,
                         ratio=ratio, directions=directions, references=refs))
    scored = [r for r in rows if r['level'] is not None]
    high = any(d['ratio'] > 1.1 for row in scored for d in row['directions'])
    # Judge underutilisation against the weakest known capacity per direction,
    # not against a fast SSD trapped behind an entirely healthy slower USB path.
    path_ratios = []
    for direction in ('read', 'write'):
        parts = [d for row in scored for d in row['directions'] if d['direction'] == direction]
        if parts:
            path_ratios.append(measured[direction] / min(d['bound'] for d in parts))
    low = any(r < .4 for r in path_ratios)
    complete = len(scored) == 3 and all(len(r['directions']) == 2 for r in rows)
    weakest = min(scored, key=lambda r: (sum(d['bound'] for d in r['directions']) / len(r['directions']),
                                       ('interface','controller','drive').index(r['component']))) if scored else None
    limits = [sum(d['bound'] for d in r['directions']) / len(r['directions']) for r in scored]
    spread = max(limits) / min(limits) if limits else 1
    dominates = weakest and all(d['bound'] <= other['bound'] for d in weakest['directions']
                              for row in scored for other in row['directions'] if other['direction'] == d['direction'])
    status = ('high' if high else 'low' if low else 'incomplete' if not complete else
              'balanced' if spread <= 1.25 and all(r >= .6 for r in path_ratios) else
              'bottleneck' if spread > 1.25 and dominates and weakest['ratio'] >= .6 else 'normal')
    return dict(rows=rows, status=status, bottleneck=weakest['component'] if weakest else None, measured=measured)


def component_summary(result):
    measured = {}
    for direction in ('read', 'write'):
        cases = [c for c in result.get('cases', []) if c.get('name') == 'seq_' + direction + '_q']
        if len(cases) == 1:
            c = cases[0]
            if (c.get('status') == 'completed' and c.get('metrics_complete') is True and c.get('qd') == 16
                    and c.get('bs') == 1048576 and c.get('rw') == direction):
                measured[direction] = c.get('metrics', {}).get(direction, {}).get('mb_s')
    bounds = [f['mb_s'] for link in result.get('hardware', {}).get('links', []) if (f := link_reference(link))]
    return utilisation_summary(measured, component_capabilities(result), bounds, result.get('target', {}).get('scope') == 'physical')
