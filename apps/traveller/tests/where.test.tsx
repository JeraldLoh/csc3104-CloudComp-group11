import { fireEvent, render, screen } from "@testing-library/react";
import { WhereSheet } from "../src/components/WhereSheet";
import { netsim } from "../src/lib/netsim";
import { fetchQuestion } from "../src/lib/where";

const reply = (body: unknown, status = 200) =>
  async (_url: string | URL | Request) => new Response(JSON.stringify(body), { status });

describe("fetchQuestion", () => {
  afterEach(() => netsim.set(false));

  it("returns a waiting question", async () => {
    const fetchImpl = vi.fn(reply({ requestId: "W-1", askedAt: "2026-10-12T08:00:00Z" }));
    expect(await fetchQuestion("U-042", fetchImpl)).toEqual({ requestId: "W-1", askedAt: "2026-10-12T08:00:00Z" });
    expect(fetchImpl.mock.calls[0][0]).toContain("/api/where-are-you?travellerId=U-042");
  });

  it("returns null when nobody is asking", async () => {
    expect(await fetchQuestion("U-042", reply(null))).toBeNull();
  });

  it("returns null when it can't check, so the next check tries again", async () => {
    expect(await fetchQuestion("U-042", reply({ error: {} }, 503))).toBeNull();
    expect(await fetchQuestion("U-042", async () => { throw new TypeError("network"); })).toBeNull();
  });

  it("doesn't try with no signal", async () => {
    netsim.set(true);
    const fetchImpl = vi.fn(reply({ requestId: "W-1" }));
    expect(await fetchQuestion("U-042", fetchImpl)).toBeNull();
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("WhereSheet", () => {
  it("asks in the caregiver's name and offers two answers", () => {
    const onAnswer = vi.fn();
    render(<WhereSheet caregiver="Sam" onAnswer={onAnswer} />);

    expect(screen.getByRole("heading").textContent).toBe("Sam is asking where you are");
    fireEvent.click(screen.getByRole("button", { name: "I'm OK" }));
    fireEvent.click(screen.getByRole("button", { name: "I need help" }));
    expect(onAnswer.mock.calls).toEqual([["ok"], ["help"]]);
  });

  it("starts a sentence with a capital when there's no caregiver name", () => {
    render(<WhereSheet caregiver="your caregiver" onAnswer={() => {}} />);
    expect(screen.getByRole("heading").textContent).toBe("Your caregiver is asking where you are");
  });
});
