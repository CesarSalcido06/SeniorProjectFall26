from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE

NAVY = RGBColor(0x14, 0x18, 0x2b)
ACCENT = RGBColor(0xe0, 0x5a, 0x2e)
BLUE = RGBColor(0x1f, 0x5c, 0x9e)
GRAY = RGBColor(0x4a, 0x4a, 0x4a)
WHITE = RGBColor(0xff, 0xff, 0xff)
PLACEHOLDER = RGBColor(0x99, 0x33, 0x00)

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
blank = prs.slide_layouts[6]


def add_slide():
    return prs.slides.add_slide(blank)


def set_bg(slide, color):
    bg = slide.background
    bg.fill.solid()
    bg.fill.fore_color.rgb = color


def add_title(slide, text, color=NAVY):
    box = slide.shapes.add_textbox(Inches(0.7), Inches(0.4), Inches(11.9), Inches(1.0))
    p = box.text_frame.paragraphs[0]
    r = p.add_run()
    r.text = text
    r.font.size = Pt(34)
    r.font.bold = True
    r.font.color.rgb = color
    line = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.72), Inches(1.28), Inches(1.6), Pt(4))
    line.fill.solid(); line.fill.fore_color.rgb = ACCENT; line.line.fill.background()


def add_accent_bar(slide):
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(0.22), prs.slide_height)
    bar.fill.solid(); bar.fill.fore_color.rgb = ACCENT; bar.line.fill.background()


def add_bold_bullets(slide, left, top, width, height, items, size=23, lead_color=ACCENT, body_color=NAVY, gap=16):
    """items: list of (lead, rest) tuples. lead is bold, rest is normal, same line."""
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    for i, (lead, rest) in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(gap)
        r1 = p.add_run()
        r1.text = f"●  {lead}"
        r1.font.size = Pt(size)
        r1.font.bold = True
        r1.font.color.rgb = lead_color
        if rest:
            r2 = p.add_run()
            r2.text = f"  {rest}"
            r2.font.size = Pt(size)
            r2.font.bold = False
            r2.font.color.rgb = body_color
    return box


def add_placeholder_note(slide, text, top=Inches(6.7)):
    box = slide.shapes.add_textbox(Inches(0.7), top, Inches(11.9), Inches(0.6))
    p = box.text_frame.paragraphs[0]
    r = p.add_run()
    r.text = f"[FILL IN: {text}]"
    r.font.size = Pt(14)
    r.font.italic = True
    r.font.bold = True
    r.font.color.rgb = PLACEHOLDER


def section_divider(title, subtitle=None):
    s = add_slide()
    set_bg(s, NAVY)
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.9), Inches(2.75), Inches(1.6), Pt(6))
    bar.fill.solid(); bar.fill.fore_color.rgb = ACCENT; bar.line.fill.background()
    box = s.shapes.add_textbox(Inches(0.9), Inches(3.0), Inches(11.5), Inches(1.2))
    p = box.text_frame.paragraphs[0]
    r = p.add_run(); r.text = title
    r.font.size = Pt(44); r.font.bold = True; r.font.color.rgb = WHITE
    if subtitle:
        box2 = s.shapes.add_textbox(Inches(0.9), Inches(4.15), Inches(11.5), Inches(0.6))
        p2 = box2.text_frame.paragraphs[0]
        r2 = p2.add_run(); r2.text = subtitle
        r2.font.size = Pt(20); r2.font.color.rgb = RGBColor(0xd8, 0xd8, 0xd8)
    return s


def title_slide(title, subtitle, footer):
    s = add_slide(); set_bg(s, NAVY)
    box = s.shapes.add_textbox(Inches(0.9), Inches(2.5), Inches(11.5), Inches(1.4))
    p = box.text_frame.paragraphs[0]
    r = p.add_run(); r.text = title
    r.font.size = Pt(46); r.font.bold = True; r.font.color.rgb = WHITE
    box = s.shapes.add_textbox(Inches(0.9), Inches(3.75), Inches(11.5), Inches(0.7))
    p = box.text_frame.paragraphs[0]
    r = p.add_run(); r.text = subtitle
    r.font.size = Pt(20); r.font.color.rgb = RGBColor(0xd8, 0xd8, 0xd8)
    box = s.shapes.add_textbox(Inches(0.9), Inches(6.7), Inches(11.5), Inches(0.5))
    p = box.text_frame.paragraphs[0]
    r = p.add_run(); r.text = footer
    r.font.size = Pt(15); r.font.bold = True; r.font.color.rgb = RGBColor(0xaa, 0xaa, 0xaa)
    return s


def closing_slide(title, subtitle):
    s = add_slide(); set_bg(s, NAVY)
    box = s.shapes.add_textbox(Inches(0.9), Inches(3.0), Inches(11.5), Inches(1.1))
    p = box.text_frame.paragraphs[0]
    r = p.add_run(); r.text = title
    r.font.size = Pt(42); r.font.bold = True; r.font.color.rgb = WHITE
    box = s.shapes.add_textbox(Inches(0.9), Inches(4.05), Inches(11.5), Inches(0.6))
    p = box.text_frame.paragraphs[0]
    r = p.add_run(); r.text = subtitle
    r.font.size = Pt(20); r.font.color.rgb = RGBColor(0xd8, 0xd8, 0xd8)


# 1 — Title
title_slide(
    "Photo-to-Game-Asset Pipeline",
    "From real-world capture — photo, drone, or sensor scan — to a ready-to-use game asset",
    "COMP 490  •  SENIOR PROJECT I  •  FALL 2026",
)

# 2 — Project Summary
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "Project Summary")
add_bold_bullets(s, Inches(0.75), Inches(1.75), Inches(11.6), Inches(4.9), [
    ("Capture.", "Phone photo, drone flyover, or sensor scan of a real object."),
    ("Process.", "Classify it, generate a 3D mesh/texture/rig, auto-import into a target engine — Unity first."),
    ("Two open questions we're answering.", "Do we run generation through a third-party AI service or build our own? And what capture hardware feeds the pipeline beyond a phone photo?"),
    ("Hardware track.", "Drones, LiDAR, and thermal/visual mapping are an active side effort that plug into this pipeline — or stand alone as their own mapping service."),
])

# 3 — Breaking down the problem
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "Breaking Down the Problem")
add_bold_bullets(s, Inches(0.75), Inches(1.75), Inches(11.6), Inches(4.9), [
    ("01 — Capture.", "What medium do we use to scan the real-world object and get it onto the user's device?"),
    ("02 — Reconstruct & rig.", "How do we turn that scan into a 3D model and get it rigged, ready for Unity?"),
    ("03 — Import & configure.", "How do we get the asset into Unity with physics, colliders, and rigid body attributes added automatically?"),
], size=23)

# 4 — Hardware divider
section_divider("Hardware", "Capture, processing, and where it can go")

# 5 — Hardware overview
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "Hardware Overview")
add_bold_bullets(s, Inches(0.75), Inches(1.8), Inches(11.6), Inches(4.3), [
    ("What we capture", "sets the ceiling on output quality — a single phone photo is the MVP baseline."),
    ("Beyond that,", "we're evaluating drone-based multi-angle scanning, plus LiDAR/thermal/visual mapping."),
    ("The key decision:", "run reconstruction through Meshy's API, or build that step ourselves."),
], size=24)

# 6 — Branch A: Build it ourselves
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "Branch A — Build the Pipeline Ourselves", color=BLUE)
add_bold_bullets(s, Inches(0.75), Inches(1.75), Inches(11.6), Inches(4.9), [
    ("Capture app", "scans the object via the device camera and produces a basic 3D model in the browser."),
    ("Blender script", "takes that basic model and auto-rigs it, setting up bones and weights for Unity."),
    ("Unity script", "imports the rigged asset and automatically adds physics, colliders, and rigid body components."),
    ("What this actually is:", "classical scripting/automation against Blender's and Unity's own APIs — no model training required for the rigging/import half."),
], size=20, lead_color=BLUE, gap=14)

# 7 — Limitations
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "Where This Breaks Down", color=BLUE)
add_bold_bullets(s, Inches(0.75), Inches(1.7), Inches(11.6), Inches(4.9), [
    ("The hard part isn't rigging —", "Blender's own tools handle that well once we script it."),
    ("It's reconstruction:", "turning a single 2D photo into an accurate 3D mesh is a real research problem, not a scripting problem."),
    ("Building that from scratch", "needs large 3D datasets and real GPU budget — beyond a one-semester scope."),
    ("No open-source tool", "currently matches a commercial single-photo-to-mesh service for quality, out of the box."),
], size=20, lead_color=BLUE, gap=14)

# 8 — Branch B: Meshy
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "Branch B — Using Meshy.ai for Reconstruction", color=ACCENT)
add_bold_bullets(s, Inches(0.75), Inches(1.7), Inches(11.6), Inches(4.9), [
    ("One API call", "handles the hard reconstruction step — mesh, texture, and a starting rig — that we're not trying to build ourselves."),
    ("Our Blender/Unity scripts still do the rest:", "cleanup, final rigging, and automated Unity import stay our own work either way."),
    ("Trade-offs:", "per-generation credit cost, rate limits, and depending on a third party's uptime and roadmap."),
    ("This is the MVP path.", "A fully in-house reconstruction step stays on the table as future/stretch work."),
], size=20, gap=14)

# 8 — Drones
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "Hardware — Drone Capture")
add_bold_bullets(s, Inches(0.75), Inches(1.7), Inches(11.6), Inches(3.6), [
    ("Why drones:", "flyover capture for objects too large, elevated, or hard to reach for a handheld phone scan."),
    ("Same pipeline:", "multi-angle footage feeds the same classify → generate steps, just with richer coverage."),
    ("Status:", "already in progress as a side effort — plugs in as an alternate capture front-end."),
], size=22)
add_placeholder_note(s, "drone platform/model, flight software, and what's already working on the side project")

# 9 — LiDAR / Thermal / Visual mapping
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "Hardware — LiDAR / Thermal / Visual Mapping")
add_bold_bullets(s, Inches(0.75), Inches(1.7), Inches(11.6), Inches(2.6), [
    ("Adds", "depth data (LiDAR), heat signature (thermal), and standard visual capture on top of a single photo."),
], size=22)
box = s.shapes.add_textbox(Inches(0.95), Inches(3.5), Inches(11.2), Inches(2.7))
tf = box.text_frame; tf.word_wrap = True
p = tf.paragraphs[0]
r = p.add_run(); r.text = "1.  Built into the app"
r.font.size = Pt(22); r.font.bold = True; r.font.color.rgb = ACCENT
p2 = tf.add_paragraph()
r = p2.add_run(); r.text = "Richer sensor data improves reconstruction fidelity for the same pipeline."
r.font.size = Pt(20); r.font.color.rgb = NAVY
p2.space_after = Pt(18)
p3 = tf.add_paragraph()
r = p3.add_run(); r.text = "2.  Subsidiary service"
r.font.size = Pt(22); r.font.bold = True; r.font.color.rgb = ACCENT
p4 = tf.add_paragraph()
r = p4.add_run(); r.text = "Mapping/scanning offered as its own product, sharing infrastructure but serving non-gaming use cases too."
r.font.size = Pt(20); r.font.color.rgb = NAVY
add_placeholder_note(s, "specific sensors/mapping software already in use on the side project")

# 10 — Toolkit
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "Toolkit")
add_bold_bullets(s, Inches(0.75), Inches(1.75), Inches(5.9), Inches(4.6), [
    ("Frontend:", "React (capture app)"),
    ("3D preview:", "three.js"),
    ("Backend:", "Node.js or Python API + job queue"),
    ("Desktop app:", "Electron or Tauri"),
], size=21)
add_bold_bullets(s, Inches(6.85), Inches(1.75), Inches(5.7), Inches(4.6), [
    ("Unity:", "C#, Unity Editor API"),
    ("Blender (stretch):", "Python, bpy"),
    ("Unreal (stretch):", "Unreal Python API"),
    ("Hardware/mapping:", "drone + LiDAR SDKs — TBD"),
], size=21)

# 11 — Algorithms
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "Algorithms")
add_bold_bullets(s, Inches(0.75), Inches(1.7), Inches(11.6), Inches(4.9), [
    ("Classification:", "pretrained vision model or vision-LLM call sorts captures into prop / creature / character, setting generation parameters."),
    ("Generation (MVP):", "Meshy API handles reconstruction, PBR texturing, and auto-rigging."),
    ("Generation (future candidates):", "photogrammetry or NeRF-based reconstruction + a separate rigging tool, benchmarked against Meshy."),
    ("Sensor fusion (LiDAR + thermal + visual):", "approach TBD, depends on hardware side-project specifics."),
], size=20, gap=14)

# 12 — Code & Architecture
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "Code & Architecture")
box = s.shapes.add_textbox(Inches(0.75), Inches(1.7), Inches(11.7), Inches(1.5))
tf = box.text_frame; tf.word_wrap = True
p = tf.paragraphs[0]
r = p.add_run()
r.text = "Capture (phone / drone / sensor)  →  Backend (classify + orchestrate)  →  Desktop hub (queue, preview, target picker)  →  Engine import (Unity / Blender / Unreal)"
r.font.size = Pt(19); r.font.bold = True; r.font.color.rgb = NAVY
add_bold_bullets(s, Inches(0.75), Inches(3.4), Inches(11.6), Inches(3), [
    ("Repo layout:", "capture app, backend orchestrator, classifier module, desktop hub, per-engine import packages."),
    ("Hardware hook-in:", "drone/LiDAR/thermal code plugs in at the capture stage as an alternate front-end to the same backend."),
], size=21)

# 13 — Evaluation
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "How We'll Know It Works")
add_bold_bullets(s, Inches(0.75), Inches(1.8), Inches(11.6), Inches(4.3), [
    ("Classification accuracy —", "how often the object gets sorted correctly."),
    ("End-to-end success rate —", "how many test captures come out as a working, unbroken Unity asset."),
    ("Time per asset —", "how long it takes, start to finish, versus doing it by hand."),
    ("Definition of done —", "a finished Unity game object with physics, rigging, and colliders already set up, ready to drop into a scene."),
], size=21, gap=14)

# 14 — Timeline
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "Timeline")
rows = [
    ("Week of Sep 29", "Validate Meshy API end-to-end, lock MVP scope"),
    ("October", "Capture app, backend, classifier, desktop app"),
    ("Early Nov", "Unity import automation"),
    ("Mid Nov", "Full pipeline test; hardware integration spike"),
    ("Late Nov", "Stretch targets (Blender/Unreal, in-house client eval)"),
    ("Early Dec", "Final demo + writeup"),
]
top = Inches(1.75)
for label, desc in rows:
    b1 = s.shapes.add_textbox(Inches(0.75), top, Inches(2.6), Inches(0.65))
    p = b1.text_frame.paragraphs[0]; r = p.add_run(); r.text = label
    r.font.bold = True; r.font.size = Pt(19); r.font.color.rgb = ACCENT
    b2 = s.shapes.add_textbox(Inches(3.5), top, Inches(8.8), Inches(0.65))
    p = b2.text_frame.paragraphs[0]; r = p.add_run(); r.text = desc
    r.font.size = Pt(19); r.font.bold = True; r.font.color.rgb = NAVY
    top += Inches(0.82)

# 15 — What we expect to learn
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "What We Expect to Learn")
add_bold_bullets(s, Inches(0.75), Inches(1.7), Inches(11.6), Inches(4.9), [
    ("Generalization:", "how well existing reconstruction/rigging tools hold up across object types they weren't built for."),
    ("Build vs. buy:", "whether an in-house client would ever pay off, or Meshy remains the right call."),
    ("Hardware ROI:", "where richer capture (drone, LiDAR, thermal) meaningfully improves output vs. just adding complexity."),
    ("Failure modes:", "where the pipeline breaks most, and what that tells us about where to invest next."),
], size=21, gap=14)

# 16 — Team
s = add_slide(); set_bg(s, WHITE); add_accent_bar(s)
add_title(s, "Team")
add_bold_bullets(s, Inches(0.75), Inches(1.8), Inches(11.6), Inches(4.3), [
    ("Cesar Salcido", "— Phone/capture app + backend API"),
    ("Daniel Silvaz", "— Desktop app GUI + Unity Editor package"),
], size=25)

# 17 — Closing
closing_slide("Questions?", "Photo-to-Game-Asset Pipeline")

prs.save("presentation.pptx")
print("saved presentation.pptx —", len(prs.slides._sldIdLst), "slides")
