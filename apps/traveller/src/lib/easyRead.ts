// How a step's wording is shown: one short action per line, as the caregiver saw it.
//
// The Easy-read Service writes one action per sentence, and the caregiver's editor shows (and saves) each
// sentence as a bullet point (apps/caregiver/src/planner/format.ts, easyReadPoints). The traveller sees the same
// points in the same order, split by the same rule, so what the caregiver checked is exactly what the traveller reads.
// Both apps' tests run the cases in contracts/examples/easy-read-points.json: change the rule there and in both apps.
import type { Landmark } from "../types";

// A full stop after these is part of a name ("St. Andrew's Village"), not the end of a sentence.
const NOT_AN_END = /\b(?:St|Mt|Dr|Rd|Ave|No|Jln|Blk|Opp)\.$/i;

/** "Get on bus 190. Tap your card!" -> ["Get on bus 190", "Tap your card!"] (same rule as the caregiver app). */
export function easyReadPoints(text: string | null | undefined): string[] {
  const points: string[] = [];
  for (const part of (text ?? "").split(/(?<=[.!?])\s+/)) {
    if (points.length && NOT_AN_END.test(points[points.length - 1])) points[points.length - 1] += ` ${part}`;
    else points.push(part);
  }
  return points.map((p) => p.trim().replace(/\.$/, "")).filter(Boolean);
}

/** Words that start a sentence: "your caregiver" -> "Your caregiver". A name stays as it is. */
export function sentenceStart(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * The words under a landmark. With its own photo: "Look for Oasis Terraces" (still right if that photo can't be
 * shown). Without one, the Journey Service left it out because it would look the same as another photo
 * (`sameView`: "step" or that landmark's name), so say where to look instead, in the caregiver app's words. If the
 * pack doesn't say which photo, "the photo above" is still right: a landmark is only left without a photo when an
 * earlier photo already shows it.
 */
export function landmarkCaption(m: Landmark, stepHasPhoto: boolean): string {
  const look = `Look for ${m.name}`;
  if (m.photoUrl) return look;
  if (m.sameView === "step") return stepHasPhoto ? `${look}: it is in the step photo above.` : `${look}.`;
  if (m.sameView) return `${look}: it is in the photo of ${m.sameView}.`;
  return stepHasPhoto ? `${look}: it is in the photo above.` : `${look}.`;
}
