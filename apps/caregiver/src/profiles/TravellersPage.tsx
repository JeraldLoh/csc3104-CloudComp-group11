// Travellers (issue 5.1): the logged-in caregiver's travellers, their safe locations, and adding another
// caregiver. The Alert Service only ever returns this caregiver's own travellers.
import { useEffect, useState, type FormEvent } from "react";
import {
  addCaregiver,
  addSafeLocation,
  addTraveller,
  listCaregivers,
  listSafeLocations,
  removeTraveller,
  type SafeLocation,
  type Traveller,
} from "./api";
import { isCaregiverId, type MyTravellers } from "./useMyTravellers";
import "./profiles.css";

interface Props {
  caregiverId: string;
  my: MyTravellers;
  /** Start with this traveller open (when opened from the dashboard). */
  initialTravellerId?: string | null;
}

export function TravellersPage({ caregiverId, my, initialTravellerId }: Props) {
  const [selectedId, setSelectedId] = useState<string | null>(initialTravellerId ?? null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  if (!isCaregiverId(caregiverId)) {
    return (
      <div className="pf">
        <div className="pf__inner">
          <div className="pf__placeholder">
            <p className="pf__placeholder-title">Who's signed in?</p>
            <p>Enter a caregiver ID such as <code>G-001</code> in <strong>Act as</strong>.</p>
          </div>
        </div>
      </div>
    );
  }

  const selected = my.travellers.find((t) => t.travellerId === selectedId) ?? null;

  async function add(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const created = await addTraveller(caregiverId, name.trim());
      setName("");
      await my.reload();
      setSelectedId(created.travellerId);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pf">
      <div className="pf__inner">
        <header className="pf__head">
          <h1 className="pf__h1">Travellers</h1>
          <p className="pf__sub">
            People you look after, signed in as <strong>{caregiverId}</strong>. Only caregivers linked to a traveller
            can see them or their alerts.
          </p>
        </header>

        <div className="pf__grid">
          <section className="pf__card">
            <div className="pf__card-head">
              <h2>My travellers</h2>
              <span className="pf__count">{my.travellers.length}</span>
            </div>
            {my.error && <p className="pf__msg pf__msg--error" role="alert">{my.error}</p>}
            {my.loaded && my.travellers.length === 0 && <p className="pf__muted">No travellers yet. Add the first one below.</p>}
            <ul className="pf__people">
              {my.travellers.map((t) => (
                <li key={t.travellerId}>
                  <button type="button" className="pf__person" aria-pressed={t.travellerId === selectedId}
                    onClick={() => setSelectedId(t.travellerId)}>
                    <Avatar name={t.name} />
                    <span className="pf__person-text">
                      <strong>{t.name}</strong>
                      <span className="pf__muted">{t.travellerId}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            <form className="pf__form" onSubmit={add}>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="New traveller's name" aria-label="New traveller's name" />
              <button type="submit" className="pf__btn" disabled={busy || !name.trim()}>Add</button>
            </form>
            {error && <p className="pf__msg pf__msg--error" role="alert">{error}</p>}
            {notice && <p className="pf__msg pf__msg--ok" role="status">{notice}</p>}
          </section>

          <section className="pf__card">
            {selected ? (
              <TravellerDetail key={selected.travellerId} caregiverId={caregiverId} traveller={selected}
                onRemoved={async (text) => {
                  setSelectedId(null);
                  setNotice(text);
                  await my.reload();
                }} />
            ) : (
              <div className="pf__placeholder pf__placeholder--inline">
                <p className="pf__placeholder-title">Select a traveller</p>
                <p>See their safe locations and who else looks after them.</p>
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

interface DetailProps {
  caregiverId: string;
  traveller: Traveller;
  onRemoved: (message: string) => void;
}

function TravellerDetail({ caregiverId, traveller, onRemoved }: DetailProps) {
  const [confirming, setConfirming] = useState(false);
  const [locations, setLocations] = useState<SafeLocation[]>([]);
  const [caregivers, setCaregivers] = useState<string[]>([]);
  const [place, setPlace] = useState({ name: "", lat: "", lng: "" });
  const [other, setOther] = useState("");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    listSafeLocations(caregiverId, traveller.travellerId).then(setLocations,
      (e) => setMessage({ ok: false, text: (e as Error).message }));
    listCaregivers(caregiverId, traveller.travellerId).then(setCaregivers,
      (e) => setMessage({ ok: false, text: (e as Error).message }));
  }, [caregiverId, traveller.travellerId]);

  async function addPlace(e: FormEvent) {
    e.preventDefault();
    try {
      const added = await addSafeLocation(caregiverId, traveller.travellerId,
        { name: place.name.trim(), lat: Number(place.lat), lng: Number(place.lng) });
      setLocations((current) => [...current, added].sort((a, b) => a.name.localeCompare(b.name)));
      setPlace({ name: "", lat: "", lng: "" });
      setMessage(null);
    } catch (err) {
      setMessage({ ok: false, text: (err as Error).message });
    }
  }

  async function link(e: FormEvent) {
    e.preventDefault();
    const id = other.trim();
    try {
      await addCaregiver(caregiverId, traveller.travellerId, id);
      setCaregivers((current) => [...new Set([...current, id])].sort());
      setMessage({ ok: true, text: `${id} can now see ${traveller.name} and their alerts.` });
      setOther("");
    } catch (err) {
      setMessage({ ok: false, text: (err as Error).message });
    }
  }

  async function remove() {
    try {
      const { profileDeleted } = await removeTraveller(caregiverId, traveller.travellerId);
      onRemoved(profileDeleted
        ? `${traveller.name} was removed. Nobody else looked after them, so their profile was deleted.`
        : `${traveller.name} was removed from your list. Their other caregivers still look after them.`);
    } catch (err) {
      setConfirming(false);
      setMessage({ ok: false, text: (err as Error).message });
    }
  }

  async function copyId() {
    try {
      await navigator.clipboard.writeText(traveller.travellerId);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard blocked: the ID is on screen anyway
    }
  }

  const placeValid = place.name.trim() && place.lat !== "" && place.lng !== "";
  const others = caregivers.filter((id) => id !== caregiverId);

  return (
    <>
      <div className="pf__profile">
        <Avatar name={traveller.name} large />
        <div>
          <h2 className="pf__profile-name">{traveller.name}</h2>
          <span className="pf__chip">
            {traveller.travellerId}
            <button type="button" className="pf__chip-btn" onClick={copyId} title="Copy traveller ID">
              {copied ? "Copied" : "Copy"}
            </button>
          </span>
        </div>
        {!confirming && (
          <button type="button" className="pf__btn pf__btn--danger-outline pf__profile-action" onClick={() => setConfirming(true)}>
            Remove
          </button>
        )}
      </div>

      {confirming && (
        <div className="pf__confirm" role="alertdialog" aria-label={`Remove ${traveller.name}?`}>
          <p>
            <strong>Remove {traveller.name}?</strong> You'll stop seeing them and their alerts. If no other caregiver
            looks after {traveller.name}, their profile and safe locations are deleted.
          </p>
          <div className="pf__confirm-actions">
            <button type="button" className="pf__btn pf__btn--secondary" onClick={() => setConfirming(false)}>Cancel</button>
            <button type="button" className="pf__btn pf__btn--danger" onClick={remove}>Remove {traveller.name}</button>
          </div>
        </div>
      )}

      <div className="pf__section">
        <h3>Safe locations</h3>
        <p className="pf__muted">Places {traveller.name} knows well, such as home or school.</p>
        {locations.length > 0 && (
          <ul className="pf__places">
            {locations.map((loc) => (
              <li key={loc.locationId}>
                <PinIcon />
                <strong>{loc.name}</strong>
                <a href={`https://www.google.com/maps?q=${loc.lat},${loc.lng}`} target="_blank" rel="noreferrer">
                  {loc.lat.toFixed(4)}, {loc.lng.toFixed(4)}
                </a>
              </li>
            ))}
          </ul>
        )}
        <form className="pf__form pf__form--place" onSubmit={addPlace}>
          <input value={place.name} onChange={(e) => setPlace({ ...place, name: e.target.value })} placeholder="Name, e.g. Home" aria-label="Place name" />
          <input value={place.lat} onChange={(e) => setPlace({ ...place, lat: e.target.value })} placeholder="Latitude" inputMode="decimal" aria-label="Latitude" />
          <input value={place.lng} onChange={(e) => setPlace({ ...place, lng: e.target.value })} placeholder="Longitude" inputMode="decimal" aria-label="Longitude" />
          <button type="submit" className="pf__btn" disabled={!placeValid}>Add place</button>
        </form>
      </div>

      <div className="pf__section">
        <h3>Other caregivers</h3>
        <p className="pf__muted">Give another caregiver access to {traveller.name} and their alerts.</p>
        {others.length > 0 && (
          <ul className="pf__caregivers">
            {others.map((id) => <li key={id} className="pf__chip">{id}</li>)}
          </ul>
        )}
        <form className="pf__form" onSubmit={link}>
          <input value={other} onChange={(e) => setOther(e.target.value)} placeholder="Caregiver ID, e.g. G-002" aria-label="Caregiver ID" />
          <button type="submit" className="pf__btn pf__btn--secondary" disabled={!isCaregiverId(other.trim())}>Give access</button>
        </form>
      </div>

      {message && <p className={`pf__msg ${message.ok ? "pf__msg--ok" : "pf__msg--error"}`} role="status">{message.text}</p>}
    </>
  );
}

const AVATAR_COLOURS = ["#2563eb", "#7c3aed", "#db2777", "#ea580c", "#0d9488", "#4f46e5", "#0891b2"];

export function Avatar({ name, large = false }: { name: string; large?: boolean }) {
  const initials = name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
  const colour = AVATAR_COLOURS[[...name].reduce((sum, c) => sum + c.charCodeAt(0), 0) % AVATAR_COLOURS.length];
  return <span className={`pf__avatar${large ? " pf__avatar--large" : ""}`} style={{ background: colour }} aria-hidden="true">{initials}</span>;
}

function PinIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="pf__pin">
      <path d="M12 21s-7-6.1-7-11a7 7 0 0 1 14 0c0 4.9-7 11-7 11z" />
      <circle cx="12" cy="10" r="2.5" />
    </svg>
  );
}
