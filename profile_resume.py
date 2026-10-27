"""Professional PDF export from reviewed profile facts, with no AI fabrication."""
import io
from pathlib import Path
from xml.sax.saxutils import escape

import i18n
import profile_context
import profile_photo


def quality(ctx):
    core=ctx['core']
    dimensions=[('Kontaktet',20,bool(core.get('name') and (core.get('email') or core.get('phone')))),
                ('Titulli profesional',10,bool(core.get('title'))),('Përmbledhje',15,bool(ctx['summary'])),
                ('Aftësitë',15,len(ctx['skills'])>=3),('Përvoja e punës',20,bool(ctx['experience'] or ctx['experience_text'])),
                ('Gjuhët',10,bool(ctx['languages'])),('Edukimi',5,bool(ctx['education'])),
                ('Portfolio',5,bool(ctx['portfolio'] or ctx['projects']))]
    return {'score':sum(weight for _,weight,present in dimensions if present),
            'missing':[label for label,_,present in dimensions if not present]}


def document_data(skill_limit=24, include_projects=True, include_personal=False):
    ctx=profile_context.build_context()
    ctx['quality']=quality(ctx)
    ctx['total_skills']=len(ctx['skills'])
    ctx['skills']=ctx['skills'][:max(1,min(200,skill_limit))]
    if not include_projects:ctx['projects']=[]
    if not include_personal:ctx['personal']=[]
    return ctx


def pdf(skill_limit=24, include_projects=True, include_personal=False, include_photo=True, document=None):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, KeepTogether
    import reportlab
    font_dir=Path('/usr/share/fonts/truetype/dejavu')
    font=font_dir/'DejaVuSans.ttf';bold=font_dir/'DejaVuSans-Bold.ttf'
    if not font.exists():
        font_dir=Path(reportlab.__file__).parent/'fonts';font=font_dir/'Vera.ttf';bold=font_dir/'VeraBd.ttf'
    if 'ProfileSans' not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont('ProfileSans',str(font)))
        pdfmetrics.registerFont(TTFont('ProfileSansBold',str(bold)))
    ctx=document if document is not None else document_data(skill_limit,include_projects,include_personal);core=ctx['core'];_=i18n.translate
    ink=colors.HexColor('#203a32');green=colors.HexColor('#326c58');gray=colors.HexColor('#5e6f67')
    styles={
        'name':ParagraphStyle('Name',fontName='ProfileSansBold',fontSize=23,leading=29,textColor=ink,spaceAfter=8),
        'title':ParagraphStyle('Title',fontName='ProfileSansBold',fontSize=11,leading=16,textColor=green,spaceAfter=8),
        'body':ParagraphStyle('Body',fontName='ProfileSans',fontSize=9,leading=13,textColor=ink,spaceAfter=5),
        'small':ParagraphStyle('Small',fontName='ProfileSans',fontSize=8,leading=12,textColor=gray,spaceAfter=5),
        'heading':ParagraphStyle('Heading',fontName='ProfileSansBold',fontSize=11,leading=16,textColor=green,spaceBefore=13,spaceAfter=8,keepWithNext=True),
        'role':ParagraphStyle('Role',fontName='ProfileSansBold',fontSize=9.5,leading=14,textColor=ink,spaceAfter=4,keepWithNext=True),
    }
    def p(text,style='body'):return Paragraph(escape(str(text or '')).replace('\n','<br/>'),styles[style])
    def heading(text):return p(_(text).upper(),'heading')
    flow=[];photo=profile_photo.get() if include_photo else None
    head=[p(core['name'] or _('Profili yt'),'name'),p(core['title'],'title'),p('  |  '.join(v for v in (core.get('location'),core.get('email'),core.get('phone')) if v),'small')]
    if photo:
        image=Image(io.BytesIO(photo),width=69,height=69,kind='proportional')
        table=Table([[head,image]],colWidths=[405,82]);table.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0)]));flow.append(table)
    else:flow.extend(head)
    if ctx['personal']:
        labels={'address':'Adresa','birth_date':'Datëlindja','age':'Mosha e deklaruar','nationality':'Shtetësia'}
        for entry in ctx['personal']:
            flow.append(p('  |  '.join(_(labels[k])+': '+str(v) for k,v in entry.items() if k in labels and v),'small'))
    if ctx['summary']:flow.extend([heading('Rreth meje'),p(ctx['summary'])])
    if ctx['experience']:
        flow.append(heading('Përvoja e punës'))
        for entry in ctx['experience']:
            flow.extend([p(entry['title'],'role'),p('  |  '.join(entry.get(k,'') for k in ('org','period') if entry.get(k)),'small')])
            if entry.get('description'):flow.append(p(entry['description']))
            flow.append(Spacer(1,5))
    elif ctx['experience_text']:flow.extend([heading('Përvoja e punës'),p(ctx['experience_text'])])
    for section,label in [('education','Edukimi'),('certifications','Certifikimet')]:
        if ctx[section]:
            entries=['  |  '.join(str(entry.get(k) or '') for k in ('title','name','org','period','year','details') if entry.get(k)) for entry in ctx[section]]
            if section=='certifications':
                flow.extend([heading(label),p(' · '.join(entries))])
            else:
                flow.extend([heading(label),*[p(entry) for entry in entries]])
    if ctx['skills']:flow.extend([heading('Aftësi & teknologji'),p(' · '.join(e['name'] for e in ctx['skills']))])
    if ctx['languages']:flow.extend([heading('Gjuhët'),p(' · '.join(' — '.join([e['name'],e.get('level','')]).strip(' —') for e in ctx['languages']))])
    if ctx['projects']:
        flow.append(heading('Projektet'))
        names=[]
        for entry in ctx['projects']:
            if not any(entry.get(k) for k in ('description','stack','url')):
                names.append(entry['title']);continue
            flow.append(p(entry['title'],'role'))
            if entry.get('description'):flow.append(p(entry['description']))
            if entry.get('stack'):flow.append(p(entry['stack'],'small'))
            if entry.get('url'):flow.append(p(entry['url'],'small'))
        if names:flow.append(p(' · '.join(names)))
    if ctx['portfolio']:
        flow.append(heading('Portfolio'))
        for entry in ctx['portfolio']:
            title,url=entry['title'],entry.get('url','')
            flow.append(p(url if title.removeprefix('https://').removeprefix('http://').rstrip('/')==url.removeprefix('https://').removeprefix('http://').rstrip('/') else '  |  '.join([title,url]).strip(' |')))
    if ctx['interests']:flow.extend([heading('Hobi & interesa'),p(' · '.join(e['name'] for e in ctx['interests']))])
    def footer(canvas,doc):
        canvas.saveState();canvas.setStrokeColor(colors.HexColor('#dce5dc'));canvas.line(54,42,541,42)
        canvas.setFont('ProfileSans',7);canvas.setFillColor(gray)
        canvas.drawString(54,29,core['name'][:65]);canvas.drawRightString(541,29,str(doc.page));canvas.restoreState()
    output=io.BytesIO()
    doc=SimpleDocTemplate(output,pagesize=A4,rightMargin=54,leftMargin=54,topMargin=45,bottomMargin=58,
                          title=core['name']+' — CV',author=core['name'])
    doc.build(flow,onFirstPage=footer,onLaterPages=footer)
    return output.getvalue()
