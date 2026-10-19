import json

import main


def create_case(client, case_id='SQL-CASE-001', with_image=False, png_file=None):
    files = {'image': ('slide.png', png_file, 'image/png')} if with_image else None
    response = client.post('/api/cases', data={'case_id':case_id,'description':'SQL Server integration test'}, files=files)
    assert response.status_code == 201, response.text
    return response.json()


def test_public_status_and_auth_boundary(client):
    anonymous = type(client)(main.app)
    assert anonymous.get('/api/status').status_code == 200
    assert anonymous.get('/api/cases').status_code == 401
    status = client.get('/api/status')
    assert status.status_code == 200
    assert 'openslide_available' in status.json()


def test_csrf_is_enforced(client):
    header = client.headers.pop('X-CSRF-Token')
    try:
        assert client.post('/api/cases', data={'case_id':'BLOCKED','description':''}).status_code == 403
    finally:
        client.headers['X-CSRF-Token'] = header


def test_user_password_is_hashed(client):
    response = client.post('/api/users', json={'username':'manager','display_name':'Manager','role':'Laboratory Manager','password':'safe-password-123'})
    assert response.status_code in (200, 201), response.text
    with main.connect() as db:
        columns = {row['name'].lower() for row in db.execute("SELECT COLUMN_NAME AS name FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='users'")}
        user = db.execute('SELECT password_hash FROM users WHERE username=?', ('manager',)).fetchone()
    assert 'password' not in columns
    assert user['password_hash'] != 'safe-password-123'


def test_protected_system_admin_cannot_be_demoted_or_deleted(client):
    reserved = client.post('/api/users', json={'username':'ETDDev','display_name':'ETD Developer','role':'System Administrator','password':'safe-password-123'})
    extra_super_admin = client.post('/api/users', json={'username':'third-super','display_name':'Third Super','role':'Super Administrator','password':'safe-password-123'})
    assert reserved.status_code == 403
    assert extra_super_admin.status_code == 400
    with main.connect() as db:
        cursor = db.execute(
            "INSERT INTO users (username,display_name,role,password_hash,is_active) VALUES (?,?,?,?,1)",
            ('ETDDev','ETD Developer','System Administrator',main.get_password_hash('safe-password-123')),
        )
        user_id = cursor.lastrowid
        db.commit()
    demote = client.patch(f'/api/users/{user_id}/role', json={'role': 'Laboratory Manager'})
    delete = client.delete(f'/api/users/{user_id}')
    assert demote.status_code == 403
    assert delete.status_code == 403


def test_case_slide_worklist_and_legacy_image_routes(client, png_file):
    case = create_case(client, with_image=True, png_file=png_file)
    assert case['slide_count'] == 1
    listed = client.get('/api/cases', params={'search':'SQL-CASE'}); assert listed.status_code == 200
    assert any(item['id'] == case['id'] for item in listed.json())
    detail = client.get(f"/api/cases/{case['id']}"); assert detail.status_code == 200
    slide = detail.json()['slides'][0]
    validation = client.get(f"/api/slides/{slide['id']}/validation")
    assert validation.status_code == 200
    assert validation.json()['valid'] is True
    assert validation.json()['integrity'] == 'valid'
    assert validation.json()['width'] == 64
    assert validation.json()['height'] == 48
    assert validation.json()['level_count'] == 1
    assert len(validation.json()['checksum_sha256']) == 64
    reimport = client.post(f"/api/slides/{slide['id']}/versions", files={'image':('slide.png',png_file,'image/png')})
    assert reimport.status_code == 201, reimport.text
    assert reimport.json()['version'] == 2
    assert reimport.json()['validation']['duplicate_of_previous_version'] is True
    with main.connect() as db:
        version_id = db.execute('SELECT TOP 1 id FROM image_versions WHERE slide_id=? ORDER BY version DESC',(slide['id'],)).fetchone()['id']
    correction = client.patch(f'/api/platform/image-versions/{version_id}/metadata', json={'field_name':'barcode','corrected_value':'BAR-001','reason':'Verified against slide label'})
    history = client.get(f'/api/platform/image-versions/{version_id}/metadata-history')
    assert correction.status_code == 200
    assert history.status_code == 200 and history.json()[0]['corrected_value'] == 'BAR-001'
    assert client.get(f"/api/slides/{slide['id']}/image").status_code == 200
    assert client.get(f"/api/cases/{case['id']}/image").status_code == 200
    bookmark = client.post(f"/api/platform/slides/{slide['id']}/bookmarks", json={'name':'Tumor region','viewport_x':0.1,'viewport_y':0.2,'viewport_width':0.3,'viewport_height':0.25,'rotation':90})
    assert bookmark.status_code == 201, bookmark.text
    assert client.get(f"/api/platform/slides/{slide['id']}/bookmarks").json()[0]['name'] == 'Tumor region'
    assert client.delete(f"/api/platform/slides/{slide['id']}/bookmarks/{bookmark.json()['id']}").status_code == 204
    preferences = client.put(f"/api/platform/slides/{slide['id']}/viewer-preferences", json={'display_profile':'grayscale','brightness':105,'contrast':110})
    assert preferences.status_code == 200, preferences.text
    restored = client.get(f"/api/platform/slides/{slide['id']}/viewer-preferences")
    assert restored.json()['saved'] is True and restored.json()['display_profile'] == 'grayscale'
    with main.connect() as db:
        raw_settings = db.execute('SELECT settings_json FROM viewer_preferences WHERE slide_id=?',(slide['id'],)).fetchone()['settings_json']
    assert json.loads(raw_settings) == {'display_profile':'grayscale','brightness':105,'contrast':110}
    update = client.patch(f"/api/slides/{slide['id']}", json={'label':'Updated','stain':'H&E','specimen_description':'Block A'})
    assert update.status_code == 200 and update.json()['label'] == 'Updated'


def test_upload_rejects_corrupt_unsupported_and_duplicate_images(client, png_file):
    first = create_case(client, 'VALIDATION-ORIGINAL', True, png_file)
    corrupt = client.post('/api/cases', data={'case_id':'VALIDATION-CORRUPT','description':''}, files={'image':('broken.png',b'not-an-image','image/png')})
    unsupported = client.post('/api/cases', data={'case_id':'VALIDATION-UNSUPPORTED','description':''}, files={'image':('slide.bmp',png_file,'image/bmp')})
    duplicate = client.post('/api/cases', data={'case_id':'VALIDATION-DUPLICATE','description':''}, files={'image':('copy.png',png_file,'image/png')})
    assert first['slide_count'] == 1
    assert corrupt.status_code == 400
    assert unsupported.status_code == 400
    assert duplicate.status_code == 409
    with main.connect() as db:
        count = db.execute("SELECT COUNT(*) AS n FROM cases WHERE case_id IN ('VALIDATION-CORRUPT','VALIDATION-UNSUPPORTED','VALIDATION-DUPLICATE')").fetchone()['n']
    assert count == 0


def test_review_qc_and_report_lifecycle(client, png_file):
    case = create_case(client, 'SQL-WORKFLOW-001', True, png_file)
    slide = case['slides'][0]
    qc = client.post(f"/api/slides/{slide['id']}/qc", json={'qc_status':'Pass','qc_blur':False,'qc_missing_tissue':False,'qc_scan_artifact':False,'qc_incorrect_label':False,'qc_unusual_staining':True,'qc_note':'Stain variation reviewed'})
    assert qc.status_code in (200, 201), qc.text
    assert qc.json()['qc_unusual_staining'] is True
    assigned = client.patch(f"/api/cases/{case['id']}/reviewer", json={'reviewer':'Dr Test'})
    assert assigned.status_code == 200, assigned.text
    progress = client.patch(f"/api/cases/{case['id']}/status", json={'status':'In Review'})
    assert progress.status_code == 200, progress.text
    note = client.post(f"/api/cases/{case['id']}/review-notes", json={'note':'Reviewed in SQL integration test'})
    assert note.status_code in (200, 201), note.text
    report = client.post(f"/api/cases/{case['id']}/reports", json={'reviewer_name':'Dr Test'})
    assert report.status_code in (200, 201), report.text
    report_id = report.json()['id']
    template = client.post('/api/platform/report-templates', json={'name':'Standard pathology','sections':['Clinical','Microscopy','Diagnosis','AI evidence']})
    assert template.status_code == 201, template.text
    patch = client.patch(f'/api/reports/{report_id}', json={'specimen_clinical':'Specimen','gross_description':'Gross','microscopic_findings':'Microscopy','diagnosis':'Test diagnosis','comments':'Test only','reviewer_name':'Dr Test','findings':[]})
    assert patch.status_code == 200, patch.text
    pending = client.patch(f'/api/platform/reports/{report_id}/workflow', json={'status':'Pending Review','comment':'Ready for independent review'})
    approved = client.patch(f'/api/platform/reports/{report_id}/workflow', json={'status':'Approved','comment':'Evidence reviewed'})
    assert pending.status_code == approved.status_code == 200
    assert client.get(f'/api/platform/reports/{report_id}/reviews').json()[-1]['new_status'] == 'Approved'
    finalized = client.post(f'/api/reports/{report_id}/finalize')
    assert finalized.status_code == 200, finalized.text
    assert client.get(f'/api/reports/{report_id}/pdf').status_code == 200
    assert client.get(f'/api/reports/{report_id}/csv').status_code == 200
    assert client.get(f'/api/reports/{report_id}/html').status_code == 200

    with main.connect() as db:
        db.execute("INSERT INTO users (username,display_name,role,password_hash,is_active) VALUES ('consultant','Consultant','Pathologist',?,1)",(main.get_password_hash('safe-password-123'),)); db.commit()
    consultation = client.post(f"/api/platform/cases/{case['id']}/consultations", json={'consultant_username':'consultant','message':'Please review the selected region'})
    assert consultation.status_code == 201
    removed = client.delete(f"/api/platform/cases/{case['id']}")
    assert removed.status_code == 204, removed.text
    with main.connect() as db:
        assert db.execute('SELECT id FROM cases WHERE id=?',(case['id'],)).fetchone() is None
        audit_event = db.execute("SELECT TOP 1 details FROM audit_log WHERE action='case.delete' AND target_id=? ORDER BY id DESC",(str(case['id']),)).fetchone()
    assert json.loads(audit_event['details'])['finalized_report_count'] == 1


def test_extended_annotations_measurements_revisions_and_exports(client, png_file):
    case=create_case(client,'ANNOTATION-TOOLS-001',True,png_file); slide=case['slides'][0]
    line=client.post(f"/api/slides/{slide['id']}/annotations",json={'annotation_type':'line','label':'Margin','points':[[2,3],[20,30]],'category':'Pathologist','annotation_status':'Active'})
    ellipse=client.post(f"/api/slides/{slide['id']}/annotations",json={'annotation_type':'ellipse','label':'Region','x':5,'y':6,'width':20,'height':10,'category':'QC','annotation_status':'Active'})
    assert line.status_code == ellipse.status_code == 201, (line.text,ellipse.text)
    updated=client.patch(f"/api/slides/{slide['id']}/annotations/{ellipse.json()['id']}",json={'label':'Resized region','note':'Reviewed','color':'#e85d75','x':8,'y':9,'width':22,'height':12,'category':'QC','annotation_status':'Resolved','change_reason':'Adjusted to tissue boundary'})
    assert updated.status_code == 200, updated.text
    with main.connect() as db:
        revision=db.execute('SELECT TOP 1 * FROM annotation_revisions WHERE annotation_id=?',(ellipse.json()['id'],)).fetchone()
    assert revision['changed_by'] == 'admin'
    geojson=client.get(f"/api/platform/slides/{slide['id']}/annotations/export",params={'format':'geojson'})
    csv_export=client.get(f"/api/platform/slides/{slide['id']}/annotations/export",params={'format':'csv'})
    assert geojson.status_code == 200 and geojson.json()['coordinate_system'].startswith('level-0 image pixels')
    assert csv_export.status_code == 200 and 'annotation_id' in csv_export.text
    deleted=client.delete(f"/api/slides/{slide['id']}/annotations/{ellipse.json()['id']}")
    assert deleted.status_code == 204, deleted.text
    with main.connect() as db:
        assert db.execute('SELECT id FROM annotation_revisions WHERE annotation_id=?',(ellipse.json()['id'],)).fetchone() is None
        deletion_audit=db.execute("SELECT TOP 1 * FROM audit_log WHERE action='annotation.delete' AND target_id=? ORDER BY id DESC",(str(ellipse.json()['id']),)).fetchone()
    assert deletion_audit is not None
    removed_slide=client.delete(f"/api/platform/slides/{slide['id']}")
    assert removed_slide.status_code == 204, removed_slide.text
    with main.connect() as db:
        assert db.execute('SELECT id FROM slides WHERE id=?',(slide['id'],)).fetchone() is None
        slide_audit=db.execute("SELECT TOP 1 * FROM audit_log WHERE action='slide.delete' AND target_id=? ORDER BY id DESC",(str(slide['id']),)).fetchone()
    assert slide_audit is not None
    removed_case=client.delete(f"/api/platform/cases/{case['id']}")
    assert removed_case.status_code == 204, removed_case.text
    with main.connect() as db:
        assert db.execute('SELECT id FROM cases WHERE id=?',(case['id'],)).fetchone() is None
        case_audit=db.execute("SELECT TOP 1 * FROM audit_log WHERE action='case.delete' AND target_id=? ORDER BY id DESC",(str(case['id']),)).fetchone()
    assert case_audit is not None


def test_identifier_matching_reconciliation_and_ai_governance(client, png_file, tmp_path, monkeypatch):
    case = create_case(client, 'MATCH-CASE-001', True, png_file)
    source = tmp_path / 'incoming.png'
    source.write_bytes(png_file + b'unique-ingestion-copy')
    monkeypatch.setenv('WATCHED_FOLDER', str(tmp_path))
    matched = client.post('/api/platform/ingestion/jobs', json={'source_path':str(source),'match_identifier':'MATCH-CASE-001'})
    assert matched.status_code == 202, matched.text
    assert matched.json()['status'] == 'Queued'
    assert matched.json()['matched_case_id'] == case['id']
    paused = client.post('/api/platform/ingestion/batch', json={'job_ids':[matched.json()['id']],'action':'pause'})
    resumed = client.post('/api/platform/ingestion/batch', json={'job_ids':[matched.json()['id']],'action':'resume'})
    assert paused.json()['updated'] == [matched.json()['id']]
    assert resumed.json()['updated'] == [matched.json()['id']]

    unmatched_source = tmp_path / 'unmatched.png'
    unmatched_source.write_bytes(png_file + b'unmatched-copy')
    unmatched = client.post('/api/platform/ingestion/jobs', json={'source_path':str(unmatched_source),'match_identifier':'UNKNOWN-ACCESSION'})
    assert unmatched.status_code == 202
    assert unmatched.json()['status'] == 'Quarantined'
    reconciliation = client.get('/api/platform/reconciliation').json()[0]
    resolved = client.patch(f"/api/platform/reconciliation/{reconciliation['id']}/resolve", json={'resolved_case_id':case['id']})
    assert resolved.status_code == 200
    jobs = client.get('/api/platform/ingestion/jobs').json()
    resolved_job = next(item for item in jobs if item['id'] == unmatched.json()['id'])
    assert resolved_job['status'] == 'Queued'
    assert resolved_job['matched_case_id'] == case['id']
    assert resolved_job['match_method'] == 'manual-reconciliation'

    research = client.post('/api/platform/ai/models', json={'name':'Research model','version':'1','checksum':'research-checksum','task':'segmentation','approval_status':'Research'})
    validated = client.post('/api/platform/ai/models', json={'name':'Validated model','version':'1','checksum':'validated-checksum','task':'segmentation','approval_status':'Validated'})
    slide_id = case['slides'][0]['id']
    with main.connect() as db:
        db.execute("UPDATE users SET role='Pathologist' WHERE username='admin'")
        db.commit()
    try:
        blocked = client.post('/api/platform/ai/jobs', json={'model_id':research.json()['id'],'slide_id':slide_id,'parameters':{'threshold':0.5}})
        allowed = client.post('/api/platform/ai/jobs', json={'model_id':validated.json()['id'],'slide_id':slide_id,'parameters':{'threshold':0.5}})
        assert blocked.status_code == 409
        assert allowed.status_code == 202
    finally:
        with main.connect() as db:
            db.execute("UPDATE users SET role='System Administrator' WHERE username='admin'")
            db.commit()


def test_platform_health_schema_and_authorization(client):
    health = client.get('/api/platform/health')
    assert health.status_code == 200, health.text
    assert health.json()['database']['ok'] is True
    assert client.get('/api/platform/ingestion/jobs').status_code == 200
    assert client.get('/api/platform/ai/models').status_code == 200
    assert client.get('/api/platform/hl7/events').status_code == 200
    assert client.get('/api/platform/backups').status_code == 200


def test_case_management_hierarchy_search_timeline_and_governance(client, png_file):
    first = create_case(client, 'CASE-MGMT-001', True, png_file)
    second = create_case(client, 'CASE-MGMT-002')
    patient = client.post('/api/platform/patients', json={'external_id':'PAT-001','display_label':'Authorized patient label'})
    assert patient.status_code == 201, patient.text
    accession = client.post('/api/platform/accessions', json={
        'case_id':first['id'],'patient_id':patient.json()['id'],'accession_number':'ACC-001','order_id':'ORD-001','priority':'Urgent'
    })
    specimen = client.post('/api/platform/specimens', json={
        'accession_id':accession.json()['id'],'specimen_code':'SP-001','specimen_type':'Biopsy','description':'Core biopsy'
    })
    block = client.post('/api/platform/blocks', json={'specimen_id':specimen.json()['id'],'block_code':'BLK-001'})
    assert accession.status_code == specimen.status_code == block.status_code == 201
    slide_id=first['slides'][0]['id']
    hierarchy = client.patch(f'/api/platform/slides/{slide_id}/hierarchy', json={'block_id':block.json()['id'],'slide_identifier':'SLIDE-001','scanner_name':'Scanner-A'})
    assert hierarchy.status_code == 200, hierarchy.text
    with main.connect() as db:
        db.execute("UPDATE slides SET stain='H&E' WHERE id=?",(slide_id,))
        db.commit()

    configured = client.patch(f"/api/platform/cases/{first['id']}/configuration", json={
        'status':'In Review','reviewer':'Dr Test','priority':'Stat','exception_reason':'Expedite'
    })
    assert configured.status_code == 200, configured.text
    searched = client.get('/api/platform/cases/search', params={'identifier':'ACC-001','specimen_type':'Biopsy','stain':'H&E','scanner':'Scanner-A','assigned_user':'Dr Test'})
    assert searched.status_code == 200 and searched.json()[0]['id'] == first['id']
    linked = client.post(f"/api/platform/cases/{first['id']}/links", json={'related_case_id':second['id'],'link_type':'Prior','reason':'Verified accession history'})
    assert linked.status_code == 201
    assert client.get(f"/api/platform/cases/{first['id']}/links").json()[0]['related_case_id'] == second['id']
    timeline = client.get(f"/api/platform/cases/{first['id']}/timeline")
    assert timeline.status_code == 200
    assert {'case-created','slide-ingested','operation'}.issubset({event['event_type'] for event in timeline.json()})

    folder = client.post('/api/platform/research-folders', json={'name':'Study A','allowed_roles':['Pathologist']})
    assert folder.status_code == 201, folder.text
    member = client.post(f"/api/platform/research-folders/{folder.json()['id']}/members", json={'case_id':first['id']})
    assert member.status_code == 201

    split = client.post('/api/platform/cases/split', json={'source_case_id':first['id'],'new_case_id':'CASE-MGMT-SPLIT','slide_ids':[slide_id],'reason':'Specimen belongs to separate case'})
    assert split.status_code == 201, split.text
    merge = client.post('/api/platform/cases/merge', json={'source_case_id':split.json()['target_case_id'],'target_case_id':second['id'],'reason':'Authorized correction'})
    assert merge.status_code == 200, merge.text
    with main.connect() as db:
        moved = db.execute('SELECT case_id FROM slides WHERE id=?',(slide_id,)).fetchone()
        operations = db.execute("SELECT COUNT(*) AS n FROM case_operations WHERE operation_type IN ('split','merge')").fetchone()['n']
    assert moved['case_id'] == second['id']
    assert operations == 2


def test_three_role_boundaries(client):
    expectations = {
        'System Administrator': {
            '/api/cases': 200,
            '/api/platform/health': 200,
            '/api/platform/ingestion/jobs': 200,
            '/api/platform/ai/models': 200,
            '/api/platform/hl7/events': 200,
            '/api/platform/settings': 200,
            '/api/platform/audit': 200,
            '/api/users': 200,
            '/api/patients': 200,
        },
        'Laboratory Manager': {
            '/api/cases': 200,
            '/api/platform/health': 200,
            '/api/platform/ingestion/jobs': 200,
            '/api/patients': 200,
            '/api/platform/ai/models': 403,
            '/api/platform/hl7/events': 403,
            '/api/platform/settings': 403,
            '/api/platform/audit': 403,
            '/api/users': 403,
        },
        'Pathologist': {
            '/api/cases': 200,
            '/api/platform/ai/models': 200,
            '/api/patients': 200,
            '/api/platform/ingestion/jobs': 403,
            '/api/platform/hl7/events': 403,
            '/api/platform/settings': 403,
            '/api/platform/audit': 403,
            '/api/users': 403,
        },
    }
    try:
        for role, routes in expectations.items():
            with main.connect() as db:
                db.execute('UPDATE users SET role=? WHERE username=?', (role, 'admin'))
                db.commit()
            for route, expected_status in routes.items():
                response = client.get(route)
                assert response.status_code == expected_status, f'{role} {route}: {response.text}'
    finally:
        with main.connect() as db:
            db.execute("UPDATE users SET role='System Administrator' WHERE username='admin'")
            db.commit()


def test_pathologist_case_access_is_limited_to_assignments(client):
    case = create_case(client, case_id='ASSIGNMENT-SCOPE')
    client.patch(f"/api/cases/{case['id']}/reviewer", json={'reviewer': 'Another Pathologist'})
    with main.connect() as db:
        db.execute("UPDATE users SET role='Pathologist' WHERE username='admin'")
        db.commit()
    try:
        assert all(item['id'] != case['id'] for item in client.get('/api/cases').json())
        assert client.get(f"/api/cases/{case['id']}").status_code == 403
        with main.connect() as db:
            db.execute("UPDATE cases SET reviewer='Test Administrator' WHERE id=?", (case['id'],))
            db.commit()
        assert any(item['id'] == case['id'] for item in client.get('/api/cases').json())
        assert client.get(f"/api/cases/{case['id']}").status_code == 200
    finally:
        with main.connect() as db:
            db.execute("UPDATE users SET role='System Administrator' WHERE username='admin'")
            db.commit()
