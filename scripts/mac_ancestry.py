"""Target-bound IORegistry facts. Input stays in memory; no inventory is exported."""
import re


def target_ancestors(registry, physical_devices):
    if len(physical_devices) != 1 or not re.fullmatch(r'disk\d+', physical_devices[0]):
        return None
    found = []
    stack = [(node, ()) for node in registry] if isinstance(registry, list) else [(registry, ())]
    visited = 0
    while stack:
        node, parents = stack.pop()
        visited += 1
        if visited > 100_000 or len(parents) > 128:
            return None
        if not isinstance(node, dict):
            continue
        if node.get('BSD Name') == physical_devices[0]:
            found.append((*parents, node))
        for child in node.get('IORegistryEntryChildren', []):
            stack.append((child, (*parents, node)))
    return found[0] if len(found) == 1 else None


def mac_access_facts(registry, physical_devices):
    """Names alone, unrelated nodes and host port capability cannot bind a drive."""
    ancestors = target_ancestors(registry, physical_devices)
    if ancestors is None:
        return {}
    bridges = []
    for node in ancestors:
        label = node.get('USB Product Name') or node.get('IORegistryEntryName') or ''
        if not isinstance(label, str):
            continue
        io_class = str(node.get('IOClass') or '')
        family = 'usb4' if re.search(r'USB4', io_class, re.I) else (
            'thunderbolt' if re.search(r'Thunderbolt', io_class, re.I) else None)
        series = re.search(r'(?<![\w])(?:ASMedia\s+|ASM)246x(?![\w])', label, re.I)
        # Exact variants remain handled by the existing target-label catalog.
        if family and series:
            bridges.append((node, family, label))
    if len(bridges) != 1:
        return {}
    node, family, label = bridges[0]
    # Unit-bearing negotiated values only; bare integers have unknown units.
    speed = node.get('Link Speed') or node.get('link_speed')
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(?:Gb/s|Gbps|Gbit/s)\s*', str(speed or ''), re.I)
    gbps = float(match[1]) if match and float(match[1]) in (10, 20, 40, 80) else None
    name = 'USB4' if family == 'usb4' else 'Thunderbolt'
    return {'controller': {'model': 'ASMedia 246x', 'reported_name': label,
                           'precision': 'series', 'source': 'ioreg:target-ancestor'},
            'access_interface': {'name': name + (f' {gbps:g} Gbps' if gbps else ''),
                                 'source': 'ioreg:target-ancestor'},
            'link': {'kind': 'usb' if family == 'usb4' else 'thunderbolt',
                     'state': 'reported', 'source': 'ioreg:target-ancestor', 'gbps': gbps}}
