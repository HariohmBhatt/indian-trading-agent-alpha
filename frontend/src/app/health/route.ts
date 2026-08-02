export const dynamic = "force-dynamic";

export async function GET() {
  return Response.json({
    status: "ok",
    service: "indian-trading-agent-frontend",
    release_sha: process.env.TRADING_AGENT_RELEASE_SHA ?? "unknown",
  });
}
