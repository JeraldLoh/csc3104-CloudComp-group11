import { render, screen } from "@testing-library/react";
import { HelpSheet } from "../src/components/HelpSheet";
import type { RequestQueueState } from "../src/lib/requests";

const stuck: RequestQueueState = {
  pending: [{ id: 1, method: "POST", path: "/api/help", body: {}, label: "help", createdAt: 0, attempts: 2 }],
  lastError: "help: timed out",
  lastDone: null,
};

describe("HelpSheet", () => {
  it("with a phone number: Call is the main action once help is stuck", () => {
    render(<HelpSheet askedAt={0} requests={stuck} caregiver="Sam" phone="+65 9123 4567" onClose={() => {}} />);
    expect(screen.getByRole("heading").textContent).toBe("Call Sam now");
    expect(screen.getByRole("link").getAttribute("href")).toBe("tel:+6591234567");
  });

  it("no phone number yet (Profiles not ready): no Call button, asks someone nearby instead", () => {
    render(<HelpSheet askedAt={0} requests={stuck} caregiver="your caregiver" phone={null} onClose={() => {}} />);
    expect(screen.getByRole("heading").textContent).toBe("Ask someone nearby for help");
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(/staff member or someone near you/)).toBeTruthy();
  });

  it("delivered: says the caregiver has been told, with a capital letter", () => {
    const done: RequestQueueState = { pending: [], lastError: null, lastDone: { label: "help", at: 5 } };
    render(<HelpSheet askedAt={0} requests={done} caregiver="your caregiver" phone={null} onClose={() => {}} />);
    expect(screen.getByRole("heading").textContent).toBe("Your caregiver has been told");
  });
});
