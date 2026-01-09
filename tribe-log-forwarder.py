require("dotenv").config();
const { Client, GatewayIntentBits } = require("discord.js");
const fs = require("fs");
const path = require("path");

// ✅ Your target channel (Valkyrie tribe logs)
const TARGET_CHANNEL_ID = "1449304199270502420";

// --- ANSI helpers ---
// Discord ANSI colors: 30–37 (fg), 90–97 (bright fg).
// Purple isn't truly available, so we approximate "purple" using magenta (35 / 95).
const ANSI = {
  reset: "\u001b[0m",
  red: "\u001b[31m",
  green: "\u001b[32m",
  yellow: "\u001b[33m",
  magenta: "\u001b[35m",
  gray: "\u001b[90m",
};

// --- Event detection (tweak keywords to match your exact log formats) ---
function classifyLog(line) {
  const l = line.toLowerCase();

  // Claiming (purple)
  if (
    l.includes("claimed") ||
    l.includes("unclaimed") ||
    l.includes("tribe of") && l.includes("has joined") // optional
  ) return "CLAIM";

  // Taming (green)
  if (l.includes("tamed") || l.includes("taming")) return "TAME";

  // Deaths (red)
  if (l.includes("killed") || l.includes("was killed") || l.includes("died")) return "DEATH";

  // Demolished (yellow)
  if (l.includes("demolished") || l.includes("destroyed") || l.includes("decayed")) return "DEMO";

  return "OTHER";
}

function colorize(line) {
  const type = classifyLog(line);

  switch (type) {
    case "CLAIM":
      return `${ANSI.magenta}${line}${ANSI.reset}`;
    case "TAME":
      return `${ANSI.green}${line}${ANSI.reset}`;
    case "DEATH":
      return `${ANSI.red}${line}${ANSI.reset}`;
    case "DEMO":
      return `${ANSI.yellow}${line}${ANSI.reset}`;
    default:
      return line; // default color
  }
}

// Splits into chunks that fit Discord's 2000-char message limit.
// We wrap each chunk in ```ansi ... ```
function buildAnsiMessages(lines) {
  const messages = [];
  let buf = "```ansi\n";

  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;

    const colored = colorize(line);

    // +1 for newline, +3 for closing ```
    if ((buf.length + colored.length + 1 + 3) > 2000) {
      buf += "```";
      messages.push(buf);
      buf = "```ansi\n";
    }

    buf += colored + "\n";
  }

  if (buf !== "```ansi\n") {
    buf += "```";
    messages.push(buf);
  }

  return messages;
}

// --- Example log source: tail a local file ---
// Replace this with your actual per-tribe log capture pipeline.
const LOG_FILE = path.join(__dirname, "valkyrie-tribe.log");

// Basic file-tail (polling). For high volume, use a real tail library or stream.
let lastSize = 0;
function readNewLogLines() {
  if (!fs.existsSync(LOG_FILE)) return [];
  const stats = fs.statSync(LOG_FILE);
  const size = stats.size;

  // file rotated/truncated
  if (size < lastSize) lastSize = 0;

  const fd = fs.openSync(LOG_FILE, "r");
  const buf = Buffer.alloc(size - lastSize);
  fs.readSync(fd, buf, 0, buf.length, lastSize);
  fs.closeSync(fd);

  lastSize = size;

  const text = buf.toString("utf8");
  return text.split(/\r?\n/).filter(Boolean);
}

// --- Discord client ---
const client = new Client({
  intents: [GatewayIntentBits.Guilds],
});

client.once("ready", async () => {
  console.log(`Logged in as ${client.user.tag}`);

  const channel = await client.channels.fetch(TARGET_CHANNEL_ID);
  if (!channel || !channel.isTextBased()) {
    console.error("Target channel not found or not a text channel.");
    process.exit(1);
  }

  console.log(`Sending Valkyrie tribe logs to #${channel.name} (${TARGET_CHANNEL_ID})`);

  // Poll every 2 seconds
  setInterval(async () => {
    try {
      const newLines = readNewLogLines();
      if (newLines.length === 0) return;

      const msgs = buildAnsiMessages(newLines);

      for (const m of msgs) {
        await channel.send({ content: m });
      }
    } catch (err) {
      console.error("Error sending logs:", err);
    }
  }, 2000);
});

client.login(process.env.DISCORD_TOKEN);