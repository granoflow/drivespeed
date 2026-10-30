"""Public hardware evidence and typed storage; never persists transport JSON."""
import hashlib
import json
from datetime import date
from urllib.parse import urlsplit

LINK_FIELDS = ('kind','state','source','gt_s','lanes','max_gt_s','max_lanes','gbps')
SPEC_FIELDS = ('component','model','interface','source_url','source_label','checked_on','conditions')
REF_FIELDS = ('metric','context_sha256','value','t1','t2','t3','t4','upper_bound_mb_s','source_url','source_label','checked_on','rationale')
CAP_FIELDS = ('component','model','read_mb_s','write_mb_s','basis','source_url','source_label','checked_on','conditions')
COLLECTIONS = (('links',LINK_FIELDS),('specs',SPEC_FIELDS),('references',REF_FIELDS),('capabilities',CAP_FIELDS))


def condition_digest(payload, metric):
    name='sustained' if metric=='post_cache_mb_s' else metric
    cases=[case for case in payload['cases'] if case['name']==name]
    if len(cases)!=1:
        return None
    case=cases[0]
    storage=dict(payload['storage']);free=storage.pop('free_percent')
    storage['free_percent_bucket']=None if free is None else int(free/10)
    links=[]
    for link in payload.get('assessment',{}).get('links',[]):
        links.append({key: (int(round(link[key]*1000)) if key in ('gt_s','max_gt_s','gbps') and link[key] is not None else link[key]) for key in LINK_FIELDS})
    context=dict(hardware=payload['hardware'],storage=storage,environment=payload['environment'],plan=payload['plan'],
                 method=payload['tool']['method_version'],links=links,
                 workload={key:case[key] for key in ('name','rw','bs_bytes','qd','numjobs','runtime_limit_seconds','rwmixread')})
    if payload['tool']['method_version'] == 'drivespeed-auto-full-v1':
        context['quality'] = payload.get('quality')
    return hashlib.sha256(json.dumps(context,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def _source(item):
    url=urlsplit(item.get('source_url',''))
    return (url.scheme=='https' and bool(url.hostname) and not url.username and not url.password
            and len(item['source_url'])<=512 and isinstance(item.get('source_label'),str) and 0<len(item['source_label'])<=120)


def project_assessment(result,payload):
    from performance_rating import performance_ratings, interface_catalog, positive, component_capabilities
    hardware=result.get('hardware',{})
    if 'links' not in hardware:
        return None  # Do not change legacy frozen snapshots on a software update.
    assessment=dict(version=1,links=[],specs=[],references=[])
    for link in hardware['links'][:12]:
        if link.get('kind') not in ('pcie','usb','sata') or link.get('state') not in ('reported','negotiated'):
            continue
        entry={key:link.get(key) for key in LINK_FIELDS}
        entry['source']='system' if str(link.get('source','')).startswith(('sysfs:','system_profiler:')) or link.get('source') == 'ioreg:target-ancestor' else 'user'
        for key in ('gt_s','lanes','max_gt_s','max_lanes','gbps'):
            value=entry[key]
            if not positive(value) or value>1000000000 or key in ('lanes','max_lanes') and (int(value)!=value or value>32):
                entry[key]=None
            elif key in ('lanes','max_lanes'):
                entry[key]=int(value)
        assessment['links'].append(entry)
    refs=result.get('references') or {}
    try:
        checked=date.fromisoformat(refs.get('checked_at','')).isoformat()
    except (ValueError,TypeError):
        checked=None
    if refs.get('for_drive_model')==hardware.get('drive',{}).get('model') and checked:
        for spec in refs.get('hardware_specs',[]):
            if spec.get('model')!=payload['hardware'].get(spec.get('component'),{}).get('model'):
                continue
            if not _source(spec) or len(spec['interface'])>160 or len(spec['conditions'])>400:
                continue
            assessment['specs'].append({**{key:spec[key] for key in SPEC_FIELDS if key!='checked_on'},'checked_on':checked})
    catalog=interface_catalog()
    for chip in catalog.get('controllers',[]):
        if chip['model']==payload['hardware']['controller']['model'] and not any(s['component']=='controller' for s in assessment['specs']):
            assessment['specs'].append(dict(component='controller',model=chip['model'],interface=chip['interface'],source_url=chip['source_url'],source_label='ASMedia',checked_on=catalog['checked_at'],conditions=chip['model']))
    assessment['specs']=assessment['specs'][:12]
    capabilities=[c for c in component_capabilities(result) if _source(c) and len(c['conditions'])<=400]
    if capabilities:
        assessment['capabilities']=capabilities
    public={**payload,'assessment':assessment}
    # Preserve reproducible legacy evidence. New component utilisation uses
    # the recorded historical specification, not a moving display-time expiry.
    for row in (performance_ratings(result, today=date.fromisoformat(checked)) if checked else []):
        if row['level'] is None:
            continue
        ref=row['reference'];bands=ref['rating_bands']
        if not _source(ref) or len(bands['rationale'])>600:
            continue
        checksum=condition_digest(public,row['metric'])
        if checksum:
            assessment['references'].append(dict(metric=row['metric'],context_sha256=checksum,value=ref['value'],
                thresholds_mb_s=bands['thresholds_mb_s'],upper_bound_mb_s=bands['upper_bound_mb_s'],source_url=ref['source_url'],
                source_label=ref['source_label'],checked_on=refs['checked_at'],rationale=bands['rationale']))
    return assessment


def validate_assessment(assessment,hardware=None):
    from community_data import CommunityError
    seen=set()
    for item in assessment['specs']+assessment['references']+assessment.get('capabilities',[]):
        if not _source(item):
            raise CommunityError('validation_failed',['assessment.source_url'])
    for ref in assessment['references']:
        if ref['metric'] in seen or any(a>=b for a,b in zip(ref['thresholds_mb_s'],ref['thresholds_mb_s'][1:])) or ref['thresholds_mb_s'][-1]>ref['upper_bound_mb_s']:
            raise CommunityError('validation_failed',['assessment.references'])
        seen.add(ref['metric'])
    used=set()
    for cap in assessment.get('capabilities',[]):
        if (cap['read_mb_s'] is None and cap['write_mb_s'] is None or cap['component']=='drive' and cap['basis']!='rated' or
                hardware is not None and cap['model']!=hardware[cap['component']]['model']):
            raise CommunityError('validation_failed',['assessment.capabilities'])
        for direction in ('read','write'):
            if cap[direction+'_mb_s'] is not None:
                key=(cap['component'],direction)
                if key in used:raise CommunityError('validation_failed',['assessment.capabilities'])
                used.add(key)


def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS assessment_presence(owner_id TEXT REFERENCES reports(id) ON DELETE CASCADE,report_id TEXT,version INTEGER NOT NULL,PRIMARY KEY(owner_id,report_id))')
    for kind,fields in COLLECTIONS:
        text={'kind','state','source','component','model','interface','source_url','source_label','checked_on','conditions','metric','context_sha256','rationale','basis'}
        columns=','.join(field+(' TEXT' if field in text else ' REAL') for field in fields)
        db.execute(f'CREATE TABLE IF NOT EXISTS assessment_{kind}(owner_id TEXT,report_id TEXT,ordinal INTEGER,{columns},PRIMARY KEY(owner_id,report_id,ordinal),FOREIGN KEY(owner_id,report_id) REFERENCES assessment_presence(owner_id,report_id) ON DELETE CASCADE)')


def write(db,owner,identifier,assessment):
    if assessment is None:
        return
    db.execute('INSERT INTO assessment_presence VALUES(?,?,?)',(owner,identifier,assessment['version']))
    for kind,fields in COLLECTIONS:
        for ordinal,item in enumerate(assessment.get(kind,[])):
            row=dict(item)
            if kind=='references':
                for index,value in enumerate(row.pop('thresholds_mb_s'),1):row['t'+str(index)]=value
            values=[owner,identifier,ordinal]+[row[field] for field in fields]
            db.execute(f"INSERT INTO assessment_{kind} VALUES({','.join('?' for _ in values)})",values)


def read(db,owner,identifier):
    presence=db.execute('SELECT version FROM assessment_presence WHERE owner_id=? AND report_id=?',(owner,identifier)).fetchone()
    if presence is None:return None
    result=dict(version=presence[0])
    for kind,fields in COLLECTIONS:
        result[kind]=[]
        for stored in db.execute(f'SELECT * FROM assessment_{kind} WHERE owner_id=? AND report_id=? ORDER BY ordinal',(owner,identifier)):
            row={field:stored[field] for field in fields}
            if kind=='links':
                for field in ('lanes','max_lanes'):
                    if row[field] is not None:row[field]=int(row[field])
            if kind=='references':row['thresholds_mb_s']=[row.pop('t'+str(index)) for index in range(1,5)]
            result[kind].append(row)
        if kind=='capabilities' and not result[kind]:
            del result[kind]  # Preserve optional-field absence in old frozen packets.
    return result
