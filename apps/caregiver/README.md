# Caregiver app

Where a caregiver plans a route for a traveller. So far: the **route planner** (search two
places or click the map, see the options on the OneMap map, save one as a route) and the
traveller's **saved routes** (list, open one on the map, edit its name, easy-read wording and step
photos, simplify the wording with AI, approve) and **trips** (make one on an approved route, start, end, prepare the pack, copy the
traveller's link). The live dashboard comes later.

React + TypeScript + Vite, same tooling as `apps/traveller`. Talks to the Journey Service
(`contracts/api.md`).

## Run it

To show the whole thing (this app plus the Journey Service in Docker), follow **[DEMO.md](DEMO.md)**.

Node.js 22 or newer.

```powershell
cd apps/caregiver
npm install
npm run dev          # http://localhost:5174
```

The dev server forwards `/api` to the Journey Service on `localhost:8006` (change with
`JOURNEY_URL`). Start the service with `AUTH_MODE=dev` (or `off`), and with
`ONEMAP_MODE=recorded` if you have no OneMap token (`services/journey-service/DEMO.md`).
With `AUTH_MODE=dev` the "Act as" box in the header sets the `X-Dev-User` header.
A link like `/?from=1.4043,103.9021&to=1.3040,103.8318&mode=bus` plans straight away.

```powershell
npm test             # api client, planner logic, formatting, and a render of the component
npm run typecheck
npm run build
```

> If `npm install` fails with `Cannot read properties of null (reading 'edgesOut')`, that is a
> bug in older npm 11 releases: `npx npm@latest install` works.

## Using the planner in another page

```tsx
import { JourneyApi, RoutePlanner } from "./planner";

const api = new JourneyApi({ getToken: () => session.idToken });   // Cognito token, issue 1.5

<div style={{ height: "100vh" }}>
  <RoutePlanner api={api} travellerId="U-042" caregiverId="G-003" onSaved={(route) => ...} />
</div>
```

- Saved routes: call `useSavedRoutes(api, travellerId)` in the host (it owns the list, so it can
  refresh it after a save), give the planner a `sections` entry with id `"saved"` holding `<SavedRoutes saved={saved} />`,
  and pass `viewing={saved.opened}`. Opening a route draws it on the planner's map; planning or
  picking an option shows the plan again. `api` needs `listRoutes`, `getRoute` and `approveRoute`
  (`SavedRoutesApi`; `JourneyApi` has all of them). Changing `api` does not reload the list; pass
  a third argument (`reloadKey`) that changes with the login if it should.

```tsx
const saved = useSavedRoutes(api, "U-042");
const trips = useTrips(api, "U-042");

<RoutePlanner api={api} travellerId="U-042" caregiverId="G-003"
  viewing={saved.opened} onSaved={() => void saved.refresh()}
  sections={[
    { id: "saved", label: "Saved routes", count: saved.routes.length, content: <SavedRoutes saved={saved} trips={trips} /> },
    { id: "trips", label: "Trips", count: trips.trips.length,
      content: <Trips trips={trips} routes={saved.routes} travellerAppUrl="https://traveller.example.com" /> },
  ]} />
```

The panel is a set of tabs: **Plan** is always first, and each `sections` entry adds one after it.
Saving a route switches to the tab with id `"saved"`, where the new route now is. Inactive tabs
stay mounted, so a half-finished edit is still there when you come back.

- Editing: **Edit** on an opened route changes its name and the easy-read wording of each step (an
  emptied step gets the service's own wording). Every save makes a new version and clears the
  approval. A trip is pinned to the version it was made with, so **saving stops that route's
  planned and active trips** until the route is approved again and a new trip is made. The
  editor says how many trips are affected (pass `trips` to `<SavedRoutes>`), and the trips list
  flags such trips and hides Start, Pack and the link for them. If someone else saved first the
  service refuses (409): the page offers **Reload this route**, and nothing is overwritten.
- Simplify with AI: **Simplify with AI** on a saved route opens it and asks the service for simpler wording
  (`suggestEasyRead`, `POST /api/routes/{id}/easy-read`; it runs on CPU, so allow up to a minute). The editor then
  opens with that wording: the caregiver checks and edits each step and saves as usual (nothing is saved before).
  A step marked "standard wording" is one where the AI's text didn't keep every stop and number; if the AI is
  unavailable, every step shows the standard wording and the status line says so.
- Point form: the wording is one short action per sentence, and the saved route shows each sentence as a bullet
  point (`EasyReadPoints`, `easyReadPoints` in `format.ts`). The editor's box has one "• point" per line (Enter
  starts a new one); saving joins the lines back into sentences (`toBulletLines` / `fromBulletLines`).
- Photos: each walk and bus step shows a photo of where it ends (Street View, added by the service when a route is
  saved, if it has a Google Maps key). In the editor, **Replace photo** uploads the caregiver's own
  (`uploadStepPhoto`; JPEG, PNG or WebP up to 5 MB) and **Use Street View photo** goes back to the automatic one.
  A step with no photo (or whose link doesn't load) says what to add instead (`photoHint` in `format.ts`,
  `StepPhoto`): the gantry or platform for a train step, the entrance or gantry for a walk to a station, the bus
  stop, or where the walk ends. Street View has nothing inside stations and malls, so these need the caregiver.
  Where only an old Street View photo was found (its key is under `media/streetview/v2/rough/`, `isRoughPhoto`), the
  photo is shown with a hint under it to check it or add a better one (`roughPhotoHint`).
  Like the wording, a photo change only counts once the route is saved.
- Landmarks: after **Simplify with AI**, a step whose wording mentions a landmark ("Look out for City Plaza on your
  left") shows a smaller photo of it under the step photo, captioned "Look for City Plaza" (`LandmarkPhotos` in
  `StepPhoto.tsx`). Only the landmarks the wording still names are shown and saved (`namedLandmarks` in `format.ts`),
  so deleting the sentence removes its photo. Saved routes show them the same way.
- Deleting: **Delete** on a saved route asks first, then calls `deleteRoute(id, version)` (`SavedRoutesApi`; the
  service refuses with 409 if the route changed since the list was loaded, or if one of its trips is active). Pass
  `routesLoaded={saved.loaded}` to `<Trips>` so planned trips left without a route are flagged.
- `travellerAppUrl` is where `apps/traveller` is hosted (`VITE_TRAVELLER_APP_URL` in the dev page);
  the copyable link is `<url>/?trip=T-…&traveller=U-…`. Leave it out and no link is shown.
- Start and End are also what the traveller's phone does itself, and both are safe to repeat.

- It fills its parent, so give the parent a height. It switches to map-above-panel when its
  own width is under 700 px (container query, not the window).
- `api` is any object with the methods of `PlannerApi`, `SavedRoutesApi` and `TripsApi`, so a host
  can bring its own client or a fake.
- Styles are under `.rp` and use `--rp-*` variables; Leaflet's own CSS is imported by the map.
- Map tiles are OneMap's (`MapView.tsx`); the attribution must stay.

## Files

| File | |
|---|---|
| `src/journey/types.ts`, `api.ts` | Journey Service shapes and client (`Authorization` token or dev user, error messages) |
| `src/planner/usePlanner.ts` | all behaviour: pins, search, plan, stale answers, save |
| `src/planner/useSavedRoutes.ts`, `SavedRoutes.tsx`, `RouteEditor.tsx` | the saved-routes list: load, open, edit, approve |
| `src/planner/useTrips.ts`, `Trips.tsx` | trips: list, make, start, end, pack, traveller link, spotting trips whose route changed |
| `src/planner/RoutePlanner.tsx` | the screen; `PlaceField`, `OptionDetails`, `MapView` are its parts |
| `src/planner/format.ts` | option / live-data wording |
| `src/App.tsx` | stand-in host page (dev user, traveller id) until the real dashboard exists |

`services/journey-service/app/demo_map.html` stays as the service's own dev page; this is the
version meant to be built on.
