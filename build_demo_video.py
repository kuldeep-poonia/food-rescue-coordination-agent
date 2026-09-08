# ruff: noqa
"""Script to generate a complete, professional, high-definition 1080p demo video
with synchronized SAPI voiceover, slides, real screenshots, and captions for the
AWS "Agents for Humans" Hackathon (Good Neighbor Agents Track).
"""

import os
import wave
import subprocess
import imageio_ffmpeg
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont
import win32com.client

ARTIFACT_DIR = r"C:\Users\kuldeep\.gemini\antigravity-ide\brain\bbdfb2b1-6d94-4e21-b3a8-0c73f419d853"
FONT_PATH_REG = r"C:\Windows\Fonts\segoeui.ttf"
FONT_PATH_BOLD = r"C:\Windows\Fonts\segoeuib.ttf"
FONT_PATH_CODE = r"C:\Windows\Fonts\consola.ttf"

WIDTH, HEIGHT = 1920, 1080
FPS = 24

SCENES = [
    {
        "id": 1,
        "type": "slide",
        "badge": "AWS AGENTS FOR HUMANS HACKATHON • GOOD NEIGHBOR TRACK",
        "title": "Surplus Router: Autonomous Food Rescue",
        "subtitle": "Eliminating manual coordination busywork with Strands Agents SDK & Amazon Bedrock AgentCore",
        "bullets": [
            ("⚡ The Problem", "Nonprofit coordinators waste 3-5 hours daily on chaotic phone calls & WhatsApp texts while edible food rots in landfills."),
            ("🤝 Who It's For", "Commercial food donors, nonprofit shelters, transit volunteers, and city coordinators operating in a unified network."),
            ("🌍 Why It Matters", "Zero-delay autonomous matching prevents tons of food waste and delivers fresh meals to communities before perishability limits.")
        ],
        "narration": "Welcome to Surplus Router, an autonomous coordination agent built with the Strands Agents SDK and deployed on Amazon Bedrock AgentCore for the Good Neighbor Agents track."
    },
    {
        "id": 2,
        "type": "slide",
        "badge": "THE PERISHABLE FOOD RESCUE PARADOX",
        "title": "The Problem: Crushing Manual Busywork",
        "subtitle": "Why connecting restaurants to community shelters fails under existing manual methods",
        "bullets": [
            ("📞 3-5 Hours Daily", "Coordinators spend entire days checking fridge space, calculating driving distances, and tracking drivers manually."),
            ("⏳ 180-Minute Window", "Cooked meals and dairy spoil quickly. Any delay in manual coordination results in tons of edible food dumped into landfills."),
            ("🛑 Coordinator Burnout", "Organizers are overwhelmed by routine dispatch logistics instead of focusing on human community care.")
        ],
        "narration": "Every day, tons of fresh food are wasted while homeless shelters face shortages. Connecting them is a manual busywork nightmare: coordinators lose three to five hours daily juggling WhatsApp messages and phone calls before food spoils."
    },
    {
        "id": 3,
        "type": "slide",
        "badge": "THE AUTONOMOUS SOLUTION",
        "title": "Silent Execution, Rare Escalation",
        "subtitle": "How the Strands Agent coordinates end-to-end without requiring human micromanagement",
        "bullets": [
            ("🤖 Autonomous Routine (95%)", "The agent ingests surplus reports, evaluates regional shelter capacity, executes multi-factor matching, and dispatches drivers in milliseconds."),
            ("🛡️ Human-in-the-Loop (5%)", "The agent surfaces to the coordinator ONLY when safety boundaries are breached—such as remaining shelf-life under 60 minutes or city-wide capacity deficit."),
            ("🔒 Zero-IDOR Security", "Cryptographic capability tokens protect donor kitchens and shelter locations from unauthorized enumeration.")
        ],
        "narration": "Surplus Router eliminates this busywork. Built on the Strands Agents SDK and Amazon Bedrock AgentCore, the agent coordinates end-to-end autonomously in milliseconds, and only surfaces to humans for safety-critical exceptions."
    },
    {
        "id": 4,
        "type": "slide",
        "badge": "TECHNICAL ARCHITECTURE & AWS STACK",
        "title": "Built on AWS Serverless Primitives",
        "subtitle": "Enterprise reliability, deterministic matching math, and ACID concurrency guarantees",
        "bullets": [
            ("⚡ Strands Agents SDK", "Stateful orchestrator coordinating classification, capacity check, multi-factor matcher, and dispatch tools."),
            ("🗄️ Amazon DynamoDB", "5 tables with TransactWriteItems ACID transactions guaranteeing shelters are never double-booked or over-allocated."),
            ("📐 Deterministic Scoring", "Mathematical formula balancing Distance 35%, Capacity 25%, Dietary Fit 20%, and Expiry Urgency 20%."),
            ("📊 CloudWatch Observability", "Structured JSON logging linked by Correlation ID with automated PII sanitization before persistence.")
        ],
        "narration": "Our architecture leverages AWS primitives: Strands Agents SDK for autonomous orchestration, five Amazon DynamoDB tables with ACID transactions to prevent double-booking, and Zero-IDOR cryptographic capability tokens for total data privacy."
    },
    {
        "id": 5,
        "type": "screenshot",
        "badge": "LIVE SYSTEM DEMONSTRATION • LOCALHOST:8080",
        "title": "1. Commercial Donor Portal (Zero-IDOR Intake)",
        "image": os.path.join(ARTIFACT_DIR, "donor_portal_view_1788872888145.png"),
        "narration": "Here is our live Donor Portal. A restaurant reports 25 kilograms of prepared meals in thirty seconds. The agent instantly validates inputs, classifies perishability, and returns a cryptographic tracking receipt."
    },
    {
        "id": 6,
        "type": "screenshot",
        "badge": "LIVE SYSTEM DEMONSTRATION • LOCALHOST:8080",
        "title": "2. Recipient Partner Capacity & Dietary Check-in",
        "image": os.path.join(ARTIFACT_DIR, "recipient_partner_view_1788872958320.png"),
        "narration": "Community shelters maintain their daily intake capacity here. Through DynamoDB ACID transactions, the agent automatically deducts shelter capacity, guaranteeing zero over-allocation."
    },
    {
        "id": 7,
        "type": "screenshot",
        "badge": "LIVE SYSTEM DEMONSTRATION • LOCALHOST:8080",
        "title": "3. Autonomous Transit Volunteer Dispatch",
        "image": os.path.join(ARTIFACT_DIR, "transit_volunteer_view_1788872998915.png"),
        "narration": "The agent dispatches the nearest available volunteer driver based on vehicle type and transit window, providing exact pickup and delivery instructions with zero manual dispatch overhead."
    },
    {
        "id": 8,
        "type": "screenshot",
        "badge": "LIVE SYSTEM DEMONSTRATION • LOCALHOST:8080",
        "title": "4. Coordinator Command Center & Escalation Queue",
        "image": os.path.join(ARTIFACT_DIR, "coordinator_authenticated_view_1788873110614.png"),
        "narration": "In the Coordinator Command Center, human operators monitor live city-wide pipelines. When safety thresholds or capacity limits are breached, the agent flags an actionable escalation ticket for instant human resolution."
    },
    {
        "id": 9,
        "type": "screenshot",
        "badge": "MEASURABLE COMMUNITY IMPACT • USDA ALIGNED",
        "title": "5. Real-Time Impact Metrics & Conclusion",
        "image": os.path.join(ARTIFACT_DIR, "impact_metrics_view_1788873171685.png"),
        "narration": "Surplus Router turns food waste into community nutrition, tracking kilograms rescued and meals delivered. Built for the AWS Agents for Humans Hackathon, Good Neighbor Agents track. Thank you!"
    }
]


def generate_audio_for_scene(narration: str, out_wav_path: str) -> float:
    """Generate audio via Windows SAPI and return duration in seconds."""
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    stream.Open(out_wav_path, 3)  # 3 = SSFMCreateForWrite
    voice.AudioOutputStream = stream
    voice.Rate = 0  # Natural speed
    voice.Speak(narration)
    stream.Close()

    with wave.open(out_wav_path, "r") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return frames / float(rate)


def create_silence_wav(path: str, duration: float = 0.5, rate: int = 22050):
    """Create a short silence wav file to pad between scenes."""
    n_frames = int(rate * duration)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * n_frames)


def render_slide_frame(scene: dict) -> np.ndarray:
    """Render a 1920x1080 slide image using Pillow and convert to BGR for OpenCV."""
    img = Image.new("RGB", (WIDTH, HEIGHT), "#0b0f19")
    draw = ImageDraw.Draw(img)

    # Ambient gradients
    # Top banner bar
    draw.rectangle([0, 0, WIDTH, 80], fill="#080b12")
    draw.line([0, 80, WIDTH, 80], fill="#1e293b", width=2)

    font_badge = ImageFont.truetype(FONT_PATH_BOLD, 20)
    font_title = ImageFont.truetype(FONT_PATH_BOLD, 48)
    font_sub = ImageFont.truetype(FONT_PATH_REG, 24)
    font_card_t = ImageFont.truetype(FONT_PATH_BOLD, 26)
    font_card_d = ImageFont.truetype(FONT_PATH_REG, 20)
    font_caption = ImageFont.truetype(FONT_PATH_REG, 22)

    # Top brand & badge
    draw.text((60, 26), "🌱 Surplus Router", font=font_badge, fill="#10b981")
    draw.text((280, 26), "•", font=font_badge, fill="#64748b")
    draw.text((310, 26), scene["badge"], font=font_badge, fill="#94a3b8")

    # Main Header
    draw.text((80, 130), scene["title"], font=font_title, fill="#f8fafc")
    draw.text((80, 200), scene["subtitle"], font=font_sub, fill="#94a3b8")

    # Bullets / Cards
    bullets = scene.get("bullets", [])
    card_y = 270
    card_h = 130
    gap = 25

    for i, (btitle, bdesc) in enumerate(bullets):
        y = card_y + i * (card_h + gap)
        # Draw card container
        draw.rounded_rectangle([80, y, WIDTH - 80, y + card_h], radius=16, fill="#131b2e", outline="#243354", width=1)
        # Left highlight bar
        draw.rounded_rectangle([80, y, 92, y + card_h], radius=6, fill="#10b981")
        # Text
        draw.text((115, y + 22), btitle, font=font_card_t, fill="#10b981")
        # Word wrap desc if needed
        draw.text((115, y + 68), bdesc, font=font_card_d, fill="#cbd5e1")

    # Bottom Subtitle Banner
    draw.rectangle([0, HEIGHT - 110, WIDTH, HEIGHT], fill="#080b12")
    draw.line([0, HEIGHT - 110, WIDTH, HEIGHT - 110], fill="#10b981", width=2)
    # Speaker icon & text
    draw.text((60, HEIGHT - 75), "🎙️ Narrator: ", font=font_badge, fill="#10b981")
    draw.text((210, HEIGHT - 75), f'"{scene["narration"]}"', font=font_caption, fill="#f1f5f9")

    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def render_screenshot_frame(scene: dict) -> np.ndarray:
    """Render a 1920x1080 frame with the real UI screenshot placed in a browser container."""
    img = Image.new("RGB", (WIDTH, HEIGHT), "#0b0f19")
    draw = ImageDraw.Draw(img)

    # Top banner bar
    draw.rectangle([0, 0, WIDTH, 80], fill="#080b12")
    draw.line([0, 80, WIDTH, 80], fill="#1e293b", width=2)

    font_badge = ImageFont.truetype(FONT_PATH_BOLD, 20)
    font_title = ImageFont.truetype(FONT_PATH_BOLD, 32)
    font_caption = ImageFont.truetype(FONT_PATH_REG, 22)

    # Top brand & badge
    draw.text((60, 26), "🌱 Surplus Router", font=font_badge, fill="#10b981")
    draw.text((280, 26), "•", font=font_badge, fill="#64748b")
    draw.text((310, 26), scene["badge"], font=font_badge, fill="#94a3b8")

    # Section title
    draw.text((80, 105), scene["title"], font=font_title, fill="#f8fafc")

    # Embed Screenshot inside a browser frame
    img_path = scene["image"]
    if os.path.exists(img_path):
        shot = Image.open(img_path).convert("RGB")
        target_w = 1760
        target_h = 740
        shot_resized = shot.resize((target_w, target_h), Image.Resampling.LANCZOS)
        
        # Frame outline
        frame_x = 80
        frame_y = 165
        draw.rounded_rectangle([frame_x - 3, frame_y - 3, frame_x + target_w + 3, frame_y + target_h + 3], radius=14, fill="#1e293b", outline="#10b981", width=2)
        img.paste(shot_resized, (frame_x, frame_y))

    # Bottom Subtitle Banner
    draw.rectangle([0, HEIGHT - 110, WIDTH, HEIGHT], fill="#080b12")
    draw.line([0, HEIGHT - 110, WIDTH, HEIGHT - 110], fill="#10b981", width=2)
    draw.text((60, HEIGHT - 75), "🎙️ Narrator: ", font=font_badge, fill="#10b981")
    draw.text((210, HEIGHT - 75), f'"{scene["narration"]}"', font=font_caption, fill="#f1f5f9")

    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def main():
    print("=== Surplus Router Demo Video Generator ===")
    os.makedirs("video_scratch", exist_ok=True)
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # Step 1: Generate audio for each scene
    print("1. Generating voiceover audio for 9 scenes...")
    scene_audios = []
    scene_durations = []

    silence_path = os.path.join("video_scratch", "silence.wav")
    create_silence_wav(silence_path, duration=0.6)

    for s in SCENES:
        wav_path = os.path.join("video_scratch", f"scene_{s['id']}.wav")
        duration = generate_audio_for_scene(s["narration"], wav_path)
        scene_audios.append(wav_path)
        scene_durations.append(duration + 0.6)  # add silence buffer
        print(f"   Scene {s['id']} ({s['type']}): {duration:.2f}s audio")

    # Step 2: Combine all audio files into master_audio.wav
    print("2. Stitching master audio track...")
    concat_list_file = os.path.join("video_scratch", "audio_list.txt")
    with open(concat_list_file, "w", encoding="utf-8") as f:
        for audio_path in scene_audios:
            # Add scene audio + silence
            f.write(f"file '{os.path.abspath(audio_path).replace(chr(92), '/')}'\n")
            f.write(f"file '{os.path.abspath(silence_path).replace(chr(92), '/')}'\n")

    master_audio_path = os.path.join("video_scratch", "master_audio.wav")
    subprocess.run([
        ffmpeg_exe, "-y", "-f", "concat", "-safe", "0",
        "-i", concat_list_file, "-c", "pcm_s16le", master_audio_path
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Step 3: Render video frames
    raw_video_path = os.path.join("video_scratch", "raw_video.mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    video_writer = cv2.VideoWriter(raw_video_path, fourcc, float(FPS), (WIDTH, HEIGHT))

    print("3. Rendering high-definition 1080p video frames...")
    total_frames = 0
    for idx, s in enumerate(SCENES):
        duration = scene_durations[idx]
        n_frames = int(duration * FPS)
        total_frames += n_frames
        print(f"   Rendering Scene {s['id']}: {n_frames} frames ({duration:.1f}s)...")

        if s["type"] == "slide":
            frame = render_slide_frame(s)
        else:
            frame = render_screenshot_frame(s)

        for _ in range(n_frames):
            video_writer.write(frame)

    video_writer.release()
    print(f"   Rendered total {total_frames} frames to raw video.")

    # Step 4: Merge raw video + master audio into final MP4
    final_output_mp4 = "final_demo_video.mp4"
    print(f"4. Encoding final video with H.264 & AAC into {final_output_mp4}...")
    cmd = [
        ffmpeg_exe, "-y",
        "-i", raw_video_path,
        "-i", master_audio_path,
        "-c:v", "libx264",
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        final_output_mp4
    ]
    subprocess.run(cmd, check=True)

    print(f"SUCCESS: Final video generated: {os.path.abspath(final_output_mp4)}")
    file_size_mb = os.path.getsize(final_output_mp4) / (1024 * 1024)
    print(f"File Size: {file_size_mb:.2f} MB")


if __name__ == "__main__":
    main()
