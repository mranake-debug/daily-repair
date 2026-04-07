from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file
from models import db, RepairLog, Building, RepairEvent
from datetime import datetime, date
from sqlalchemy import func, or_
from dotenv import load_dotenv
from io import BytesIO
import math
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'))
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, 'repair.db')
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.secret_key = os.environ.get("SECRET_KEY", "secret-key-12345")

# รหัสผ่าน Admin
ADMIN_PASSWORD = os.environ.get("DAILY_REPAIR_ADMIN_PASSWORD", "change-me-now")
PER_PAGE = 10


db.init_app(app)


def seed_buildings_from_logs():
    existing_names = {b.name for b in Building.query.all()}
    distinct_buildings = db.session.query(RepairLog.building).distinct().all()

    added = False
    for row in distinct_buildings:
        building_name = (row[0] or '').strip()
        if building_name and building_name not in existing_names:
            db.session.add(Building(name=building_name, total_items=0))
            added = True

    if added:
        db.session.commit()


def create_repair_event(log, event_type, event_date, title, detail=''):
    db.session.add(RepairEvent(
        repair_log_id=log.id,
        event_type=event_type,
        event_date=event_date,
        title=title,
        detail=detail
    ))


def migrate_repair_log_schema():
    columns = [row[1] for row in db.session.execute(db.text("PRAGMA table_info(repair_log)")).fetchall()]
    alter_statements = []

    if 'job_status' not in columns:
        alter_statements.append("ALTER TABLE repair_log ADD COLUMN job_status VARCHAR(16) NOT NULL DEFAULT 'open'")
    if 'final_result' not in columns:
        alter_statements.append("ALTER TABLE repair_log ADD COLUMN final_result VARCHAR(16)")
    if 'closed_date' not in columns:
        alter_statements.append("ALTER TABLE repair_log ADD COLUMN closed_date DATE")
    if 'close_note' not in columns:
        alter_statements.append("ALTER TABLE repair_log ADD COLUMN close_note TEXT")

    for statement in alter_statements:
        db.session.execute(db.text(statement))

    if alter_statements:
        db.session.commit()

    db.session.execute(db.text('''
        CREATE TABLE IF NOT EXISTS repair_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repair_log_id INTEGER NOT NULL,
            event_type VARCHAR(32) NOT NULL,
            event_date DATE NOT NULL,
            title VARCHAR(128) NOT NULL,
            detail TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(repair_log_id) REFERENCES repair_log(id)
        )
    '''))
    db.session.commit()


def apply_log_filters(query, search_text='', selected_building='', start_date='', end_date='', job_status='', initial_status=''):
    if selected_building:
        query = query.filter(RepairLog.building == selected_building)

    if search_text:
        like = f"%{search_text}%"
        query = query.filter(
            or_(
                RepairLog.building.ilike(like),
                RepairLog.item_name.ilike(like),
                RepairLog.zone.ilike(like),
                RepairLog.fault_desc.ilike(like),
                RepairLog.notes.ilike(like)
            )
        )

    if start_date:
        query = query.filter(RepairLog.date >= datetime.strptime(start_date, '%Y-%m-%d').date())

    if end_date:
        query = query.filter(RepairLog.date <= datetime.strptime(end_date, '%Y-%m-%d').date())

    if job_status:
        query = query.filter(RepairLog.job_status == job_status)

    if initial_status:
        query = query.filter(RepairLog.status == initial_status)

    return query


def apply_sorting(query, sort_by='date_desc'):
    sort_map = {
        'date_desc': [RepairLog.date.desc(), RepairLog.id.desc()],
        'date_asc': [RepairLog.date.asc(), RepairLog.id.asc()],
        'building_asc': [RepairLog.building.asc(), RepairLog.date.desc()],
        'building_desc': [RepairLog.building.desc(), RepairLog.date.desc()],
        'status_asc': [RepairLog.status.asc(), RepairLog.date.desc()],
        'status_desc': [RepairLog.status.desc(), RepairLog.date.desc()],
    }
    return query.order_by(*sort_map.get(sort_by, sort_map['date_desc']))


def paginate_query(query, page, per_page=PER_PAGE):
    total = query.count()
    total_pages = max(1, math.ceil(total / per_page)) if total else 1
    page = min(max(page, 1), total_pages)
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return items, total, total_pages, page


def build_pagination_window(page, total_pages, radius=2):
    start = max(1, page - radius)
    end = min(total_pages, page + radius)
    return range(start, end + 1)


def build_export_rows(logs):
    rows = []
    for log in logs:
        
        # Determine status text for Excel export
        status_text = ''
        if log.job_status == 'closed':
            if log.final_result == 'fixed':
                if log.status == 'unfixable':
                    status_text = 'ซ่อมเสร็จ (เคยซ่อมไม่ได้)' # Excel-friendly text
                else: # log.status == 'fixable'
                    status_text = 'ซ่อมเสร็จ'
            else: # final_result is not 'fixed' (implies 'unfixed' or None)
                status_text = 'ปิดไม่ได้ (เคยซ่อมไม่ได้)' # Excel-friendly text
        elif log.status == 'fixable':
            status_text = 'ซ่อมได้'
        else: # log.status == 'unfixable' and job_status == 'open'
            status_text = 'งานค้างซ่อมไม่ได้'

        rows.append({
            'อาคาร': log.building,
            'วันที่': log.date.strftime('%Y-%m-%d') if log.date else '',
            'ชื่อชิ้นงาน': log.item_name,
            'จำนวน': log.quantity,
            'โซน': log.zone,
            'อาการเสีย': log.fault_desc,
            'สถานะ': status_text,
            'หมายเหตุ': log.notes or ''
        })
    return rows


def admin_required():
    return session.get('admin_logged_in')


@app.context_processor
def inject_buildings():
    buildings = Building.query.order_by(Building.name.asc()).all()
    return dict(buildings=buildings, has_buildings=len(buildings) > 0)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/add', methods=['GET', 'POST'])
def add():
    buildings = Building.query.order_by(Building.name.asc()).all()

    if request.method == 'POST':
        building_id = request.form.get('building_id')
        selected_building = db.session.get(Building, int(building_id)) if building_id and building_id.isdigit() else None

        if not selected_building:
            flash('กรุณาเลือกอาคาร', 'danger')
            return render_template('add.html', building_options=buildings)

        initial_status = request.form['status']
        r = RepairLog(
            building=selected_building.name,
            date=datetime.strptime(request.form['date'], '%Y-%m-%d').date(),
            item_name=request.form['item_name'],
            quantity=request.form.get('quantity', 1),
            zone=request.form['zone'],
            fault_desc=request.form['fault_desc'],
            status=initial_status,
            notes=request.form.get('notes', ''),
            job_status='closed' if initial_status == 'fixable' else 'open',
            final_result='fixed' if initial_status == 'fixable' else None,
            closed_date=datetime.strptime(request.form['date'], '%Y-%m-%d').date() if initial_status == 'fixable' else None,
            close_note='ปิดงานอัตโนมัติ: ซ่อมได้ตั้งแต่แรก' if initial_status == 'fixable' else None
        )
        db.session.add(r)
        db.session.commit()
        create_repair_event(
            r,
            'created',
            r.date,
            'เปิดงาน',
            f"บันทึกครั้งแรก: {'ซ่อมได้' if r.status == 'fixable' else 'ซ่อมไม่ได้'}"
        )
        if r.notes:
            create_repair_event(r, 'note', r.date, 'หมายเหตุเริ่มต้น', r.notes)
        if r.job_status == 'closed':
            create_repair_event(r, 'closed', r.closed_date or r.date, 'ปิดงานอัตโนมัติ', r.close_note or 'ซ่อมได้ตั้งแต่แรก')
        db.session.commit()
        flash('บันทึกเพิ่มเรียบร้อย', 'success')
        return redirect(url_for('list_logs'))

    return render_template('add.html', building_options=buildings)


@app.route('/list/export/pdf')
def export_logs_pdf():
    if not admin_required():
        return redirect(url_for('admin_login'))

    selected_building = request.args.get('building', '').strip()
    search_text = request.args.get('search', '').strip()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    job_status = request.args.get('job_status', '').strip()
    initial_status = request.args.get('initial_status', '').strip()

    query = RepairLog.query
    query = apply_log_filters(query, search_text, selected_building, start_date, end_date, job_status, initial_status)
    logs = query.all()

    if not logs:
        flash('ไม่พบข้อมูลสำหรับส่งออก', 'warning')
        return redirect(url_for('list_logs'))

    pdf_title = "รายงานสรุปงานซ่อม"
    if selected_building:
        pdf_title += f" - อาคาร: {selected_building}"
    if start_date and end_date:
        pdf_title += f" - ช่วงวันที่: {start_date} ถึง {end_date}"
    elif start_date:
        pdf_title += f" - ตั้งแต่วันที่: {start_date}"
    elif end_date:
        pdf_title += f" - ถึงวันที่: {end_date}"

    return render_template('export_pdf.html',
                           title=pdf_title,
                           logs=build_export_rows(logs),
                           generated_at=datetime.now(),
                           buildings=Building.query.order_by(Building.name.asc()).all(),
                           include_letterhead=(request.args.get('include_letterhead') == '1'),
                           organization_name=request.args.get('organization_name', ''),
                           report_prepared_by=request.args.get('report_prepared_by', ''),
                           report_approved_by=request.args.get('report_approved_by', ''),
                           summary_job_status=job_status,
                           building=selected_building,
                           start_date=start_date,
                           end_date=end_date)


@app.route('/list')
def list_logs():
    selected_building = request.args.get('building', '').strip()
    search_text = request.args.get('search', '').strip()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    job_status = request.args.get('job_status', '').strip()
    initial_status = request.args.get('initial_status', '').strip()
    sort_by = request.args.get('sort_by', 'date_desc').strip()
    page = request.args.get('page', 1, type=int)

    query = RepairLog.query
    query = apply_log_filters(query, search_text, selected_building, start_date, end_date, job_status, initial_status)
    query = apply_sorting(query, sort_by)
    logs, total_logs, total_pages, current_page = paginate_query(query, page)

    return render_template(
        'list.html',
        logs=logs,
        total_logs=total_logs,
        current_page=current_page,
        total_pages=total_pages,
        page_numbers=build_pagination_window(current_page, total_pages),
        selected_building=selected_building,
        search_text=search_text,
        start_date=start_date,
        end_date=end_date,
        job_status=job_status,
        initial_status=initial_status,
        sort_by=sort_by
    )


@app.route('/list/export/excel')
def export_logs_excel():
    selected_building = request.args.get('building', '').strip()
    search_text = request.args.get('search', '').strip()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    job_status = request.args.get('job_status', '').strip()

    query = RepairLog.query.order_by(RepairLog.date.desc(), RepairLog.id.desc())
    query = apply_log_filters(query, search_text, selected_building, start_date, end_date, job_status)
    logs = query.all()

    try:
        import pandas as pd
    except ImportError:
        flash('ยังไม่พร้อม export Excel เพราะไม่มี pandas', 'danger')
        return redirect(url_for('list_logs'))

    df = pd.DataFrame(build_export_rows(logs))
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='repair_logs')
    output.seek(0)

    filename = f"repair_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@app.route('/report/monthly/<int:year>/<int:month>')
def monthly_report(year, month):
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    logs = RepairLog.query.filter(RepairLog.date >= start, RepairLog.date < end).all()
    total = len(logs)
    fixable = sum(1 for l in logs if l.status == 'fixable')
    unfixable = total - fixable
    target = round(total * 0.05)
    chart_data = {
        "labels": ["ซ่อมได้", "ซ่อมไม่ได้"],
        "datasets": [{
            "label": f"เดือน {month}/{year}",
            "data": [fixable, unfixable],
            "backgroundColor": ["#4caf50", "#f44336"]
        }]
    }
    return render_template('monthly_report.html', year=year, month=month,
                           total=total, fixable=fixable, unfixable=unfixable,
                           target=target, chart_data=chart_data)


@app.route('/report/yearly/<int:year>')
def yearly_report(year):
    start = date(year, 1, 1)
    end = date(year + 1, 1, 1)
    logs = RepairLog.query.filter(RepairLog.date >= start, RepairLog.date < end).all()
    total = len(logs)
    fixable = sum(1 for l in logs if l.status == 'fixable')
    unfixable = total - fixable
    month_counts = [0] * 12
    for l in logs:
        month_counts[l.date.month - 1] += 1
    chart_data = {
        "labels": [str(m + 1) for m in range(12)],
        "datasets": [{
            "label": "จำนวนชิ้นงานต่อเดือน",
            "data": month_counts,
            "backgroundColor": "#2196f3"
        }]
    }
    return render_template('yearly_report.html', year=year,
                           total=total, fixable=fixable, unfixable=unfixable,
                           chart_data=chart_data)


def format_thai_date(dt):
    thai_months = [
        'มกราคม', 'กุมภาพันธ์', 'มีนาคม', 'เมษายน', 'พฤษภาคม', 'มิถุนายน',
        'กรกฎาคม', 'สิงหาคม', 'กันยายน', 'ตุลาคม', 'พฤศจิกายน', 'ธันวาคม'
    ]
    return f"{dt.day} {thai_months[dt.month - 1]} {dt.year + 543}"


def format_percent_display(value):
    if value == 0:
        return '0.00%'
    if 0 < value < 0.01:
        return '<0.01%'
    return f'{value:.2f}%'


def get_summary_context(form_data):
    year = form_data.get('year') or str(date.today().year)
    month = form_data.get('month') or ''
    building = form_data.get('building') or ''
    summary_job_status = form_data.get('summary_job_status', '').strip()
    include_letterhead = form_data.get('include_letterhead', '') in ('1', 'true', 'on', 'yes')
    organization_name = (form_data.get('organization_name') or '').strip()
    report_prepared_by = (form_data.get('report_prepared_by') or '').strip()
    report_approved_by = (form_data.get('report_approved_by') or '').strip()
    chart_image = form_data.get('chart_image') or ''
    pie_chart_image = form_data.get('pie_chart_image') or ''

    year = int(year)
    start = date(year, 1, 1)
    end = date(year + 1, 1, 1)

    if month:
        start = date(year, int(month), 1)
        end = date(year, int(month) + 1, 1) if int(month) < 12 else date(year + 1, 1, 1)

    query = RepairLog.query.filter(RepairLog.date.between(start, end))

    if building:
        query = query.filter(RepairLog.building == building)

    if summary_job_status:
        query = query.filter(RepairLog.job_status == summary_job_status)

    logs = query.all()
    total = len(logs)
    fixable = sum(1 for l in logs if l.status == 'fixable')
    unfixable = total - fixable
    open_jobs = sum(1 for l in logs if l.job_status == 'open' and l.status == 'unfixable')
    closed_jobs = sum(1 for l in logs if l.job_status == 'closed' and l.status == 'unfixable')
    closed_fixed = sum(1 for l in logs if l.job_status == 'closed' and l.status == 'unfixable' and l.final_result == 'fixed')
    closed_unfixed = sum(1 for l in logs if l.job_status == 'closed' and l.status == 'unfixable' and l.final_result == 'unfixed')
    target = round(total * 0.05)

    total_items_sum_query = db.session.query(func.sum(Building.total_items))
    if building:
        total_items_sum_query = total_items_sum_query.filter(Building.name == building)
    total_items_sum = total_items_sum_query.scalar() or 0

    target_five_percent_items = round(total_items_sum * 0.05) if total_items_sum > 0 else 0
    over_target_items = max(unfixable - target_five_percent_items, 0)
    within_target_items = max(target_five_percent_items - unfixable, 0)
    target_status = 'ผ่าน' if total_items_sum > 0 and unfixable <= target_five_percent_items else 'ไม่ผ่าน'

    if total_items_sum > 0:
        percent_unfixable_assets = round((unfixable / total_items_sum) * 100, 2)
    else:
        percent_unfixable_assets = 0

    if total > 0:
        percent_unfixable_reports = round((unfixable / total) * 100, 2)
    else:
        percent_unfixable_reports = 0

    if not building:
        building_colors = [
            ('#1f77b4', '#9ecae1'),
            ('#ff7f0e', '#fdd0a2'),
            ('#2ca02c', '#a1d99b'),
            ('#d62728', '#fcae91'),
            ('#9467bd', '#d4b9da'),
            ('#8c564b', '#d7b5a6'),
            ('#e377c2', '#f7b6d2'),
            ('#7f7f7f', '#c7c7c7'),
            ('#bcbd22', '#dbdb8d'),
            ('#17becf', '#9edae5')
        ]

        building_summaries = []
        for building_name in sorted({l.building for l in logs}):
            building_logs = [l for l in logs if l.building == building_name]
            fixable_value = sum(1 for l in building_logs if l.status == 'fixable')
            unfixable_value = sum(1 for l in building_logs if l.status == 'unfixable')
            total_value = fixable_value + unfixable_value
            building_summaries.append({
                'name': building_name,
                'fixable': fixable_value,
                'unfixable': unfixable_value,
                'total': total_value
            })

        building_summaries.sort(key=lambda item: item['total'], reverse=True)
        building_names = [item['name'] for item in building_summaries]

        datasets = []
        for i, item in enumerate(building_summaries):
            strong_color, soft_color = building_colors[i % len(building_colors)]
            datasets.append({
                "label": f"{item['name']} ✓",
                "data": [item['fixable'] if name == item['name'] else 0 for name in building_names],
                "backgroundColor": strong_color
            })
            datasets.append({
                "label": f"{item['name']} ✗",
                "data": [item['unfixable'] if name == item['name'] else 0 for name in building_names],
                "backgroundColor": soft_color
            })

        chart_data = {
            "labels": building_names,
            "datasets": datasets
        }
    elif month:
        chart_data = {
            "labels": ["ซ่อมได้", "ซ่อมไม่ได้"],
            "datasets": [{
                "label": f"เดือน {month}/{year}",
                "data": [fixable, unfixable],
                "backgroundColor": ["#4caf50", "#f44336"]
            }]
        }
    else:
        month_counts = [0] * 12
        for l in logs:
            month_counts[l.date.month - 1] += 1
        chart_data = {
            "labels": ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."],
            "datasets": [{
                "label": "จำนวนชิ้นงานต่อเดือน",
                "data": month_counts,
                "backgroundColor": "#2196f3"
            }]
        }

    return dict(
        year=year,
        month=month,
        building=building,
        total=total,
        fixable=fixable,
        unfixable=unfixable,
        open_jobs=open_jobs,
        closed_jobs=closed_jobs,
        closed_fixed=closed_fixed,
        closed_unfixed=closed_unfixed,
        target=target,
        chart_data=chart_data,
        percent_unfixable_assets=percent_unfixable_assets,
        percent_unfixable_assets_display=format_percent_display(percent_unfixable_assets),
        percent_unfixable_reports=percent_unfixable_reports,
        percent_unfixable_reports_display=format_percent_display(percent_unfixable_reports),
        total_items_sum=total_items_sum,
        target_five_percent_items=target_five_percent_items,
        over_target_items=over_target_items,
        within_target_items=within_target_items,
        target_status=target_status,
        include_letterhead=include_letterhead,
        organization_name=organization_name,
        report_prepared_by=report_prepared_by,
        report_approved_by=report_approved_by,
        thai_generated_date=format_thai_date(date.today()),
        chart_image=chart_image,
        pie_chart_image=pie_chart_image,
        summary_job_status=summary_job_status
    )


@app.route('/summary', methods=['GET', 'POST'])
def summary_report():
    form_data = request.form if request.method == 'POST' else request.args
    context = get_summary_context(form_data)
    return render_template('summary_report.html', **context)


@app.route('/summary/export/pdf', methods=['GET', 'POST'])
def summary_report_pdf():
    form_data = request.form if request.method == 'POST' else request.args
    context = get_summary_context(form_data)
    html = render_template('summary_report_pdf.html', generated_at=datetime.now(), **context)

    try:
        from weasyprint import HTML
    except ImportError:
        return send_file(
            BytesIO(html.encode('utf-8')),
            as_attachment=True,
            download_name=f"summary_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
            mimetype='text/html'
        )

    pdf_buffer = BytesIO()
    HTML(string=html).write_pdf(pdf_buffer)
    pdf_buffer.seek(0)

    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name=f"summary_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
        mimetype='application/pdf'
    )


# ===== Admin Routes =====

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password')
        if ADMIN_PASSWORD == 'change-me-now':
            flash('กรุณาตั้งรหัสผ่านใหม่ผ่านตัวแปรแวดล้อม DAILY_REPAIR_ADMIN_PASSWORD ก่อนใช้งาน', 'danger')
        elif password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            flash('เข้าสู่ระบบ Admin สำเร็จ', 'success')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('รหัสผ่านไม่ถูกต้อง', 'danger')
    return render_template('admin_login.html', using_default_password=(ADMIN_PASSWORD == 'change-me-now'))


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    flash('ออกจากระบบ Admin แล้ว', 'info')
    return redirect(url_for('index'))


@app.route('/admin')
def admin_dashboard():
    if not admin_required():
        return redirect(url_for('admin_login'))

    selected_building = request.args.get('building', '').strip()
    search_text = request.args.get('search', '').strip()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    job_status = request.args.get('job_status', '').strip()
    initial_status = request.args.get('initial_status', '').strip()
    overdue_days = request.args.get('overdue_days', '').strip()
    sort_by = request.args.get('sort_by', 'date_desc').strip()
    requested_admin_view = request.args.get('admin_view', '').strip()
    admin_view = requested_admin_view or session.get('admin_view', 'tracked')
    session['admin_view'] = admin_view
    page = request.args.get('page', 1, type=int)

    query = RepairLog.query
    query = apply_log_filters(query, search_text, selected_building, start_date, end_date, job_status, initial_status)
    query = apply_sorting(query, sort_by)
    logs, total_logs, total_pages, current_page = paginate_query(query, page)

    all_filtered_logs = apply_log_filters(RepairLog.query, search_text, selected_building, start_date, end_date, job_status, initial_status).all()
    if overdue_days.isdigit():
        overdue_value = int(overdue_days)
        all_filtered_logs = [log for log in all_filtered_logs if log.status == 'unfixable' and log.job_status == 'open' and (date.today() - log.date).days >= overdue_value]
        logs = [log for log in logs if log.status == 'unfixable' and log.job_status == 'open' and (date.today() - log.date).days >= overdue_value]
        total_logs = len(all_filtered_logs)
        total_pages = 1
        current_page = 1
    fixable_count = sum(1 for log in all_filtered_logs if log.status == 'fixable')
    unfixable_count = total_logs - fixable_count
    open_count = sum(1 for log in all_filtered_logs if log.job_status == 'open' and log.status == 'unfixable')
    closed_count = sum(1 for log in all_filtered_logs if log.job_status == 'closed' and log.status == 'unfixable')
    normal_fixed_count = sum(1 for log in all_filtered_logs if log.status == 'fixable')

    tracked_tab_count = sum(1 for log in all_filtered_logs if log.status == 'unfixable')
    fixable_tab_count = sum(1 for log in all_filtered_logs if log.status == 'fixable')
    all_tab_count = len(all_filtered_logs)
    today_open_count = sum(1 for log in all_filtered_logs if log.status == 'unfixable' and log.job_status == 'open' and log.date == date.today())
    recent_closed_count = sum(1 for log in all_filtered_logs if log.job_status == 'closed')

    return render_template('admin_dashboard.html',
                           logs=logs,
                           selected_building=selected_building,
                           search_text=search_text,
                           start_date=start_date,
                           end_date=end_date,
                           job_status=job_status,
                           initial_status=initial_status,
                           overdue_days=overdue_days,
                           sort_by=sort_by,
                           admin_view=admin_view,
                           total_logs=total_logs,
                           fixable_count=fixable_count,
                           unfixable_count=unfixable_count,
                           open_count=open_count,
                           closed_count=closed_count,
                           normal_fixed_count=normal_fixed_count,
                           tracked_tab_count=tracked_tab_count,
                           fixable_tab_count=fixable_tab_count,
                           all_tab_count=all_tab_count,
                           today_open_count=today_open_count,
                           recent_closed_count=recent_closed_count,
                           current_page=current_page,
                           total_pages=total_pages,
                           page_numbers=build_pagination_window(current_page, total_pages))


@app.route('/admin/edit/<int:id>', methods=['GET', 'POST'])
def admin_edit(id):
    if not admin_required():
        return redirect(url_for('admin_login'))

    log = db.session.get(RepairLog, id)
    if not log:
        return redirect(url_for('admin_dashboard'))

    buildings = Building.query.order_by(Building.name.asc()).all()

    if request.method == 'POST':
        building_id = request.form.get('building_id')
        selected_building = db.session.get(Building, int(building_id)) if building_id and building_id.isdigit() else None

        if not selected_building:
            flash('กรุณาเลือกอาคาร', 'danger')
            return render_template('admin_edit.html', log=log, building_options=buildings)

        log.building = selected_building.name
        log.date = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
        log.item_name = request.form['item_name']
        log.quantity = 1
        log.zone = request.form['zone']
        log.fault_desc = request.form['fault_desc']
        log.status = request.form['status']
        log.notes = request.form.get('notes', '')

        if log.status == 'fixable':
            log.job_status = 'closed'
            log.final_result = 'fixed'
            log.closed_date = log.date
            log.close_note = 'ปิดงานอัตโนมัติ: ซ่อมได้ตั้งแต่แรก'
        elif log.final_result == 'fixed' and log.close_note == 'ปิดงานอัตโนมัติ: ซ่อมได้ตั้งแต่แรก':
            log.job_status = 'open'
            log.final_result = None
            log.closed_date = None
            log.close_note = None
        db.session.commit()
        create_repair_event(log, 'updated', log.date, 'แก้ไขข้อมูลงาน', f"อัปเดตสถานะครั้งแรกเป็น: {'ซ่อมได้' if log.status == 'fixable' else 'ซ่อมไม่ได้'}")
        db.session.commit()
        flash('แก้ไขข้อมูลเรียบร้อย', 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('admin_edit.html', log=log, building_options=buildings)


@app.route('/admin/delete/<int:id>', methods=['POST'])
def admin_delete(id):
    if not admin_required():
        return redirect(url_for('admin_login'))

    log = db.session.get(RepairLog, id)
    if not log:
        flash('ไม่พบข้อมูลที่ต้องการลบ', 'danger')
        return redirect(url_for('admin_dashboard'))

    RepairEvent.query.filter_by(repair_log_id=log.id).delete()
    db.session.delete(log)
    db.session.commit()
    flash('ลบข้อมูลเรียบร้อย', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/timeline/<int:id>')
def admin_timeline(id):
    if not admin_required():
        return redirect(url_for('admin_login'))

    log = db.session.get(RepairLog, id)
    if not log:
        flash('ไม่พบงานที่ต้องการดูรายละเอียด', 'danger')
        return redirect(url_for('admin_dashboard'))

    badge_map = {
        'created': 'primary',
        'note': 'secondary',
        'updated': 'info',
        'closed': 'success',
        'reopened': 'warning'
    }

    timeline_items = [
        {
            'id': event.id,
            'event_type': event.event_type,
            'date': event.event_date,
            'title': event.title,
            'badge_class': badge_map.get(event.event_type, 'dark'),
            'detail': event.detail or ''
        }
        for event in sorted(log.events, key=lambda e: (e.event_date, e.id))
    ]

    return render_template('admin_timeline.html', log=log, timeline_items=timeline_items)


@app.route('/admin/timeline/<int:id>/note', methods=['POST'])
def admin_timeline_note(id):
    if not admin_required():
        return redirect(url_for('admin_login'))

    log = db.session.get(RepairLog, id)
    if not log:
        flash('ไม่พบงานที่ต้องการเพิ่มบันทึก', 'danger')
        return redirect(url_for('admin_dashboard'))

    note_date = request.form.get('note_date', '').strip()
    note_title = request.form.get('note_title', '').strip() or 'บันทึกติดตาม'
    note_detail = request.form.get('note_detail', '').strip()

    if not note_date or not note_detail:
        flash('กรุณากรอกวันที่และรายละเอียดบันทึกติดตาม', 'danger')
        return redirect(url_for('admin_timeline', id=id))

    create_repair_event(
        log,
        'note',
        datetime.strptime(note_date, '%Y-%m-%d').date(),
        note_title,
        note_detail
    )
    db.session.commit()
    flash('เพิ่มบันทึกติดตามเรียบร้อย', 'success')
    return redirect(url_for('admin_timeline', id=id))


@app.route('/admin/timeline/<int:id>/event/<int:event_id>/edit', methods=['POST'])
def admin_timeline_event_edit(id, event_id):
    if not admin_required():
        return redirect(url_for('admin_login'))

    event = db.session.get(RepairEvent, event_id)
    if not event or event.repair_log_id != id:
        flash('ไม่พบบันทึก timeline ที่ต้องการแก้ไข', 'danger')
        return redirect(url_for('admin_timeline', id=id))

    if event.event_type != 'note':
        flash('แก้ไขได้เฉพาะบันทึกติดตามเท่านั้น', 'danger')
        return redirect(url_for('admin_timeline', id=id))

    note_date = request.form.get('note_date', '').strip()
    note_title = request.form.get('note_title', '').strip() or 'บันทึกติดตาม'
    note_detail = request.form.get('note_detail', '').strip()

    if not note_date or not note_detail:
        flash('กรุณากรอกวันที่และรายละเอียดบันทึกติดตาม', 'danger')
        return redirect(url_for('admin_timeline', id=id))

    event.event_date = datetime.strptime(note_date, '%Y-%m-%d').date()
    event.title = note_title
    event.detail = note_detail
    db.session.commit()
    flash('แก้ไขบันทึก timeline เรียบร้อย', 'success')
    return redirect(url_for('admin_timeline', id=id))


@app.route('/admin/timeline/<int:id>/event/<int:event_id>/delete', methods=['POST'])
def admin_timeline_event_delete(id, event_id):
    if not admin_required():
        return redirect(url_for('admin_login'))

    event = db.session.get(RepairEvent, event_id)
    if not event or event.repair_log_id != id:
        flash('ไม่พบบันทึก timeline ที่ต้องการลบ', 'danger')
        return redirect(url_for('admin_timeline', id=id))

    if event.event_type != 'note':
        flash('ลบได้เฉพาะบันทึกติดตามเท่านั้น', 'danger')
        return redirect(url_for('admin_timeline', id=id))

    db.session.delete(event)
    db.session.commit()
    flash('ลบบันทึก timeline เรียบร้อย', 'success')
    return redirect(url_for('admin_timeline', id=id))


@app.route('/admin/close/<int:id>', methods=['GET', 'POST'])
def admin_close_job(id):
    if not admin_required():
        return redirect(url_for('admin_login'))

    log = db.session.get(RepairLog, id)
    if not log:
        flash('ไม่พบงานที่ต้องการปิด', 'danger')
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        final_result = request.form.get('final_result', '').strip()
        closed_date = request.form.get('closed_date', '').strip()
        close_note = request.form.get('close_note', '').strip()

        if final_result not in ('fixed', 'unfixed'):
            flash('กรุณาเลือกผลการปิดงาน', 'danger')
            return render_template('admin_close_job.html', log=log)

        if not closed_date:
            flash('กรุณาเลือกวันที่ปิดงาน', 'danger')
            return render_template('admin_close_job.html', log=log)

        log.job_status = 'closed'
        log.final_result = final_result
        log.closed_date = datetime.strptime(closed_date, '%Y-%m-%d').date()
        log.close_note = close_note
        db.session.commit()
        create_repair_event(
            log,
            'closed',
            log.closed_date,
            'ปิดงาน',
            f"ผลการปิดงาน: {'ซ่อมเสร็จแล้ว' if final_result == 'fixed' else 'ปิดงานไม่ได้'}" + (f" | {close_note}" if close_note else '')
        )
        db.session.commit()
        flash('ปิดงานเรียบร้อย', 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('admin_close_job.html', log=log)


@app.route('/admin/reopen/<int:id>', methods=['POST'])
def admin_reopen_job(id):
    if not admin_required():
        return redirect(url_for('admin_login'))

    log = db.session.get(RepairLog, id)
    if not log:
        flash('ไม่พบงานที่ต้องการเปิดใหม่', 'danger')
        return redirect(url_for('admin_dashboard'))

    log.job_status = 'open'
    log.final_result = None
    log.closed_date = None
    log.close_note = None
    db.session.commit()
    create_repair_event(log, 'reopened', date.today(), 'เปิดงานใหม่', 'เปิดงานกลับมาติดตามอีกครั้ง')
    db.session.commit()
    flash('เปิดงานกลับมาเรียบร้อย', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/buildings', methods=['GET', 'POST'])
def admin_buildings():
    if not admin_required():
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        total_items = request.form.get('total_items', '0').strip()

        if not name:
            flash('กรุณากรอกชื่ออาคาร', 'danger')
        elif Building.query.filter_by(name=name).first():
            flash('มีชื่ออาคารนี้อยู่แล้ว', 'danger')
        else:
            try:
                total_items_value = int(total_items)
                if total_items_value < 0:
                    raise ValueError
            except ValueError:
                flash('จำนวนชิ้นงานทั้งหมดต้องเป็นเลขจำนวนเต็มตั้งแต่ 0 ขึ้นไป', 'danger')
            else:
                db.session.add(Building(name=name, total_items=total_items_value))
                db.session.commit()
                flash('เพิ่มอาคารเรียบร้อย', 'success')
                return redirect(url_for('admin_buildings'))

    search_text = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int)

    query = Building.query.order_by(Building.name.asc())
    if search_text:
        query = query.filter(Building.name.ilike(f'%{search_text}%'))

    building_list, total_buildings, total_pages, current_page = paginate_query(query, page)
    for building in building_list:
        building.repair_logs_count = RepairLog.query.filter_by(building=building.name).count()

    return render_template('admin_buildings.html',
                           building_list=building_list,
                           total_buildings=total_buildings,
                           search_text=search_text,
                           current_page=current_page,
                           total_pages=total_pages,
                           page_numbers=build_pagination_window(current_page, total_pages))


@app.route('/admin/buildings/edit/<int:id>', methods=['GET', 'POST'])
def admin_building_edit(id):
    if not admin_required():
        return redirect(url_for('admin_login'))

    building = db.session.get(Building, id)
    if not building:
        return redirect(url_for('admin_buildings'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        total_items = request.form.get('total_items', '0').strip()

        if not name:
            flash('กรุณากรอกชื่ออาคาร', 'danger')
        elif Building.query.filter(Building.name == name, Building.id != building.id).first():
            flash('มีชื่ออาคารนี้อยู่แล้ว', 'danger')
        else:
            try:
                total_items_value = int(total_items)
                if total_items_value < 0:
                    raise ValueError
            except ValueError:
                flash('จำนวนชิ้นงานทั้งหมดต้องเป็นเลขจำนวนเต็มตั้งแต่ 0 ขึ้นไป', 'danger')
            else:
                old_name = building.name
                building.name = name
                building.total_items = total_items_value

                related_logs = RepairLog.query.filter_by(building=old_name).all()
                for log in related_logs:
                    log.building = name

                db.session.commit()
                flash('แก้ไขข้อมูลอาคารเรียบร้อย', 'success')
                return redirect(url_for('admin_buildings'))

    return render_template('admin_building_edit.html', building=building)


@app.route('/admin/buildings/delete/<int:id>', methods=['POST'])
def admin_building_delete(id):
    if not admin_required():
        return redirect(url_for('admin_login'))

    building = db.session.get(Building, id)
    if not building:
        flash('ไม่พบอาคารที่ต้องการลบ', 'danger')
        return redirect(url_for('admin_buildings'))

    usage_count = RepairLog.query.filter_by(building=building.name).count()

    if usage_count > 0:
        flash(f'ลบไม่ได้ เพราะอาคารนี้ถูกใช้งานในบันทึกซ่อมแล้ว {usage_count} รายการ', 'danger')
        return redirect(url_for('admin_buildings'))

    db.session.delete(building)
    db.session.commit()
    flash('ลบอาคารเรียบร้อย', 'success')
    return redirect(url_for('admin_buildings'))


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        migrate_repair_log_schema()
        seed_buildings_from_logs()
    app.run(host='0.0.0.0', port=5000, debug=True)
