// Mock of a Workday candidate application (*.myworkdayjobs.com .../apply),
// a React wizard whose fields carry data-automation-id names. It follows the
// markup live tenants served in October 2026 (saved, cleaned, in
// test/fixtures/workday-real/): formField-* wrappers around inputs named
// "name--legalName--firstName", listbox buttons with a popup list, search-and-pick
// boxes ("selectinput"), sections grouped by their heading ("Work-Experience-section")
// with Add buttons that are submit buttons, and file-upload-input-ref.
import React, { useState, useRef, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { createPortal } from "react-dom";
import { exposeState } from "./common.js";

const S = {};
const STEPS = ["My Information", "My Experience", "Application Questions", "Voluntary Disclosures", "Self Identify", "Review"];

function Field({ id, aid, label, required, children }) {
  return (
    <div data-automation-id={"formField-" + aid}>
      <label htmlFor={id}>{label}{required ? <abbr title="required">*</abbr> : null}</label>
      <div>{children}</div>
    </div>
  );
}

function Text({ id, aid, inputAid, label, required, type = "text" }) {
  const [v, setV] = useState(S[id] || "");
  const [committed, setCommitted] = useState(S[id] || "");
  S[id] = committed;   // Workday only takes a value when the field loses focus
  return (
    <Field id={id} aid={aid} label={label} required={required}>
      <input id={id} type={type} data-automation-id={inputAid} value={v} aria-required={required ? "true" : "false"}
        onChange={(e) => setV(e.target.value)} onBlur={(e) => setCommitted(e.target.value)} />
    </Field>
  );
}

// A listbox button, as on live Workday: the button names its list with
// aria-controls; the list opens in a popup and closes on a choice or Escape.
function ListButton({ id, aid, buttonAid, label, options, required }) {
  const [v, setV] = useState(S[id] || "");
  const [open, setOpen] = useState(false);
  S[id] = v;
  const listId = id + "-list";
  return (
    <Field id={id} aid={aid} label={label} required={required}>
      <button type="button" id={id} aria-haspopup="listbox" aria-expanded={open ? "true" : "false"} aria-controls={open ? listId : undefined}
        data-automation-id={buttonAid} aria-label={`${label} ${v || "Select One"}${required ? " Required" : ""}`}
        onClick={() => setOpen((o) => !o)} onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); }}>{v || "Select One"}</button>
      {/* Live Workday keeps an unnamed text input beside each listbox button. */}
      <input type="text" tabIndex={-1} style={{ width: 2, minWidth: 0, border: 0, padding: 0 }} />
      {open ? createPortal(
        <div data-automation-id="popUpDialog" style={{ position: "fixed", top: 40, left: 40, background: "#fff", border: "1px solid #999" }}>
          <ul role="listbox" id={listId} aria-label={label} onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); }}>
            {["Select One", ...options].map((o) => (
              <li key={o} role="option" aria-selected={(v || "Select One") === o ? "true" : "false"} onClick={() => { setV(o === "Select One" ? "" : o); setOpen(false); }}>
                <div>{o}</div>
              </li>
            ))}
          </ul>
        </div>, document.body) : null}
    </Field>
  );
}

// Workday's search-and-pick box, as served by live tenants in 2026: closed, the
// input is a "selectinput" with no automation id; open, it is "searchBox" and its
// list sits in a popup outside the form. Enter searches after a short wait; a
// search with exactly one match picks it at once. A choice can be a category with
// its own list. Picks are pills in the box. A scripted Escape doesn't close the
// list; a click outside it does. `options` is a list of strings, or of
// [category, [choices]] pairs; `browse` lists the top level before any search
// ("No Items." otherwise, like School and Skills).
function Prompt({ id, aid, label, options, required, multi, browse = false }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [shown, setShown] = useState(null);   // the list on screen: [{ text, children }]
  const [chosen, setChosen] = useState(S[id] || []);
  S[id] = chosen;
  const boxRef = useRef(null), popRef = useRef(null);
  const tree = options.map((o) => (Array.isArray(o) ? { text: o[0], children: o[1].map((c) => ({ text: c })) } : { text: o }));
  const leaves = tree.flatMap((n) => n.children || [n]);
  const top = browse ? tree : [];
  useEffect(() => {
    if (!open) return undefined;
    const away = (e) => { if (!boxRef.current.contains(e.target) && !(popRef.current && popRef.current.contains(e.target))) setOpen(false); };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);
  // In a multi-select, clicking a ticked choice unticks it, as on live Workday.
  const pick = (text, toggle = false) => {
    const next = multi ? (chosen.includes(text) ? (toggle ? chosen.filter((c) => c !== text) : chosen) : [...chosen, text]) : [text];
    setChosen(next);
    S[id] = next;
    setQ("");
    setOpen(false);
  };
  const search = () => {
    const term = q.trim().toLowerCase();
    setOpen(true);
    setShown(shown || top);
    // Results come back from the server a moment later.
    setTimeout(() => {
      const hits = term ? leaves.filter((l) => l.text.toLowerCase().includes(term)) : top;
      if (term && hits.length === 1) { pick(hits[0].text); return; }
      setShown(hits);
    }, 350);
  };
  const list = shown || top;
  return (
    <Field id={id} aid={aid} label={label} required={required}>
      <div data-automation-id="multiSelectContainer" data-uxi-widget-type="multiselect">
        <div data-automation-id="multiselectInputContainer" ref={boxRef}>
          <div>
            <input id={id} data-automation-id={open ? "searchBox" : undefined} data-uxi-widget-type="selectinput" placeholder="Search"
              autoComplete="off" aria-required={required ? "true" : "false"} value={q}
              onChange={(e) => setQ(e.target.value)}
              onClick={() => { if (!open) { setOpen(true); setShown(top); } }}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); search(); } }} />
            <div data-automation-id="promptAriaInstruction" aria-hidden="true">{open ? "Expanded" : `${chosen.length} item${chosen.length === 1 ? "" : "s"} selected`}</div>
          </div>
          {chosen.length ? (
            <ul role="listbox" data-automation-id="selectedItemList" aria-label="items selected">
              {chosen.map((c) => (
                <li key={c} role="presentation"><div role="option" data-automation-id="selectedItem" aria-label={c + ", press delete to clear value."}>
                  <p data-automation-id="promptOption" data-automation-label={c}>{c}</p></div></li>
              ))}
            </ul>) : null}
          <span data-automation-id="promptIcon" aria-hidden="true" />
        </div>
      </div>
      {open ? createPortal(
        <div data-automation-id="responsiveMonikerPrompt" ref={popRef} style={{ position: "fixed", top: 60, left: 360, background: "#fff", border: "1px solid #999" }}>
          <div data-automation-id="activeListContainer" role="listbox" aria-label="Options Expanded">
            {(list.length ? list : [{ text: "No Items." }]).map((n, k) => (
              // aria-selected marks the highlighted row (the first), not a ticked one.
              <div key={n.text} role="option" data-automation-id="menuItem" aria-label={n.text + (chosen.includes(n.text) ? " checked" : " not checked")}
                aria-selected={k === 0 ? "true" : "false"}>
                {/* The click is taken inside the row, not on the row itself. */}
                <div data-automation-id="promptLeafNode" data-automation-checked={chosen.includes(n.text) ? "Checked" : "Not Checked"}
                  onClick={() => { if (n.text === "No Items.") return; if (n.children) setShown(n.children); else pick(n.text, true); }}>
                  <div data-automation-id="promptOption" data-automation-label={n.text}>{n.text}</div>
                </div>
              </div>
            ))}
          </div>
        </div>, document.body) : null}
    </Field>
  );
}

function Radio({ id, label, options }) {
  const [v, setV] = useState(S[id] || "");
  S[id] = v;
  return (
    <div data-automation-id={"formField-" + id}>
      <fieldset>
        <legend><label>{label}</label></legend>
        {options.map((o, i) => (
          <div key={o}><input type="radio" id={id + i} name={id} value={o} checked={v === o} onChange={() => setV(o)} /><label htmlFor={id + i}>{o}</label></div>
        ))}
      </fieldset>
    </div>
  );
}

function Resume() {
  const [name, setName] = useState(S.resume || "");
  S.resume = name;
  return (
    <div data-automation-id="resumeSection">
      <h3>Resume/CV</h3>
      <div data-automation-id="file-upload-drop-zone">
        <input type="file" data-automation-id="file-upload-input-ref" style={{ display: "none" }}
          onChange={(e) => setName(e.target.files[0] ? e.target.files[0].name + ":" + e.target.files[0].size : "")} />
        <button type="button" data-automation-id="select-files">Select files</button>
        {name ? <div data-automation-id="file-upload-item">{name.split(":")[0]}</div> : null}
      </div>
    </div>
  );
}

function Agreement() {
  const [v, setV] = useState(S.agreement || false);
  S.agreement = v;
  return (
    <div data-automation-id="formField-agreementCheckbox">
      <input type="checkbox" id="agree" data-automation-id="agreementCheckbox" checked={v} onChange={(e) => setV(e.target.checked)} />
      <label htmlFor="agree">I have read and consent to the terms and conditions</label>
    </div>
  );
}

const YN = ["Yes", "No"];

// A section as live Workday groups it: role=group labelled by its heading's id.
function Section({ name, title = name, note, children }) {
  const hid = title.replace(/\s+/g, "-") + "-section";
  return (
    <div role="group" aria-labelledby={hid}>
      <h4 id={hid}>{title}</h4>
      {note ? <div>{note}</div> : null}
      {children}
    </div>
  );
}

// Workday's Add and Add Another: plain <button>s, so submit buttons.
function AddButton({ onClick, label = "Add" }) {
  return <button data-automation-id="add-button" onClick={onClick}>{label}</button>;
}

// ?empty: the sections start with no rows, only a heading and an Add button
// (Workday when you apply without its resume parser), and carry no section ids.
const EMPTY = new URLSearchParams(location.search).has("empty");

// What Workday's own "Autofill with Resume" parser leaves behind: the first
// job and school mostly right, one description mangled to ".js", a wrong GPA.
if (!EMPTY) Object.assign(S, {
  "workExperience-1--jobTitle": "Full Stack Developer Intern", "workExperience-1--companyName": "Example Labs",
  "workExperience-1--location": "Dallas, TX", "workExperience-1--roleDescription": ".js",
  "workExperience-1--startDate-dateSectionMonth-input": "07", "workExperience-1--startDate-dateSectionYear-input": "2025",
  "workExperience-1--endDate-dateSectionMonth-input": "01", "workExperience-1--endDate-dateSectionYear-input": "2026",
  "education-1--schoolName": "Example State University", "education-1--gradeAverage": "4",
  "skills--skills": ["Java (Programming Language)"],
});

function Area({ id, aid, label, maxLength }) {
  const [v, setV] = useState(S[id] || "");
  const [committed, setCommitted] = useState(S[id] || "");
  S[id] = committed;
  return (
    <Field id={id} aid={aid} label={label}>
      <textarea id={id} data-automation-id={aid} maxLength={maxLength} value={v}
        onChange={(e) => setV(e.target.value)} onBlur={(e) => setCommitted(e.target.value)} />
    </Field>
  );
}

function Check({ id, aid, label }) {
  const [v, setV] = useState(S[id] || false);
  S[id] = v;
  return (
    <div data-automation-id={"formField-" + aid}>
      <input type="checkbox" id={id} data-automation-id={aid} checked={v} onChange={(e) => setV(e.target.checked)} />
      <label htmlFor={id}>{label}</label>
    </div>
  );
}

/** Workday's date widget: separate month and year spinbuttons under one label.
 * As on live Workday, digits arrive as key presses; the number is kept in
 * aria-valuenow and shown in a display beside the box, and the input itself stays empty. */
function DateParts({ id, aid, label, month = true, day = false }) {
  const part = (p, name, size) => {
    const pid = `${id}-dateSection${p}-input`;
    const [v, setV] = useState(S[pid] || "");
    S[pid] = v;
    const typed = useRef("");
    const onKeyDown = (e) => {
      if (!/^\d$/.test(e.key)) return;
      e.preventDefault();
      typed.current = (typed.current + e.key).slice(-size);
      setV(typed.current.padStart(size, "0"));
    };
    return (
      <div id={`${id}-dateSection${p}`}>
        <div aria-hidden="true" data-automation-id={`dateSection${p}-display`}>{v || (p === "Year" ? "YYYY" : p === "Month" ? "MM" : "DD")}</div>
        <input id={pid} role="spinbutton" aria-label={name} data-automation-id={`dateSection${p}-input`} size={size} value=""
          aria-valuenow={v ? String(parseInt(v, 10)) : undefined} aria-valuetext={v ? String(parseInt(v, 10)) : undefined}
          onChange={() => {}} onKeyDown={onKeyDown} onFocus={() => { typed.current = ""; }} />
      </div>
    );
  };
  return (
    <div data-automation-id={"formField-" + aid}>
      <label>{label}</label>
      <div data-automation-id="dateInputWrapper">{month ? part("Month", "Month", 2) : null}{day ? part("Day", "Day", 2) : null}{part("Year", "Year", 4)}</div>
    </div>
  );
}

// Skill names as Workday's skills list words them; the resume says "Java", "React.js", "Node.js".
const SKILL_LIST = ["Angular", "Express.js", "Java (Programming Language)", "JavaScript", "Node JS", "Python (Programming Language)",
  "React (JavaScript Library)", "SQL", "TypeScript", "Kotlin", "Go (Programming Language)", "C++ Programming Language"];

function Skills() {
  return (
    <div>
      <h3>Skills</h3>
      <Prompt id="skills--skills" aid="skills" label="Type to Add Skills" options={SKILL_LIST} multi />
    </div>
  );
}

function Languages() {
  const [n, setN] = useState(S.__langs ?? 0);
  S.__langs = n;
  const LEVELS = ["Beginner", "Intermediate", "Advanced", "Fluent"];
  return (
    <Section name="Languages">
      {Array.from({ length: n }, (_, i) => i + 1).map((k) => {
        const p = `language-${k}--`;
        return (
          <div key={k} data-automation-id={`language-${k}`}>
            <h4>Languages {k}</h4>
            <ListButton id={p + "language"} aid="language" label="Language" options={["English", "Gujarati", "Hindi", "Spanish"]} required />
            <Check id={p + "native"} aid="native" label="I am fluent in this language." />
            <ListButton id={p + "reading"} aid="reading" label="Reading" options={LEVELS} />
            <ListButton id={p + "speaking"} aid="speaking" label="Speaking" options={LEVELS} />
            <ListButton id={p + "writing"} aid="writing" label="Writing" options={LEVELS} />
          </div>
        );
      })}
      <AddButton onClick={() => setN(n + 1)} label={n ? "Add Another" : "Add"} />
    </Section>
  );
}

function Websites() {
  const [n, setN] = useState(S.__sites ?? 0);
  S.__sites = n;
  return (
    <Section name="Websites" title="LinkedIn" note="Add any relevant websites.">
      {Array.from({ length: n }, (_, i) => i + 1).map((k) => (
        <div key={k} data-automation-id={`webAddress-${k}`}>
          <Text id={`webAddress-${k}--url`} aid="url" label="URL" required />
        </div>
      ))}
      <AddButton onClick={() => setN(n + 1)} label={n ? "Add Another" : "Add"} />
    </Section>
  );
}

function Projects() {
  const [n, setN] = useState(S.__projects ?? 0);
  S.__projects = n;
  return (
    <div>
      <h3>Projects</h3>
      {Array.from({ length: n }, (_, i) => i + 1).map((k) => {
        const p = `project-${k}--`;
        return (
          <div key={k} data-automation-id={`project-${k}`}>
            <Text id={p + "projectName"} aid="projectName" label="Project Name" required />
            <DateParts id={p + "startDate"} aid="startDate" label="From" />
            <DateParts id={p + "endDate"} aid="endDate" label="To" />
            <Text id={p + "projectUrl"} aid="projectUrl" label="Project URL" />
            <Area id={p + "description"} aid="description" label="Description" maxLength={2000} />
          </div>
        );
      })}
      <AddButton onClick={() => setN(n + 1)} />
    </div>
  );
}

function Certifications() {
  const [n, setN] = useState(S.__certs ?? 0);
  S.__certs = n;
  return (
    <Section name="Certifications">
      {Array.from({ length: n }, (_, i) => i + 1).map((k) => (
        <div key={k} data-automation-id={`certification-${k}`}>
          <Text id={`certification-${k}--certification`} aid="certification" label="Certification" />
        </div>
      ))}
      <AddButton onClick={() => setN(n + 1)} />
    </Section>
  );
}

function WorkHistory() {
  const [jobs, setJobs] = useState(S.__jobs ?? (EMPTY ? 0 : 1));
  const [schools, setSchools] = useState(S.__schools ?? (EMPTY ? 0 : 1));
  S.__jobs = jobs;
  S.__schools = schools;
  const nums = (k) => Array.from({ length: k }, (_, i) => i + 1);
  return (
    <>
      <Section name="Work Experience">
        {nums(jobs).map((n) => {
          const p = `workExperience-${n}--`;
          return (
            <div key={n} role="group" aria-labelledby={`Work-Experience-${n}-panel`}>
              <h5 id={`Work-Experience-${n}-panel`}>Work Experience {n}</h5>
              <Text id={p + "jobTitle"} aid="jobTitle" label="Job Title" required />
              <Text id={p + "companyName"} aid="companyName" label="Company" required />
              <Text id={p + "location"} aid="location" label="Location" />
              <Check id={p + "currentlyWorkHere"} aid="currentlyWorkHere" label="I currently work here" />
              <DateParts id={p + "startDate"} aid="startDate" label="From" />
              <DateParts id={p + "endDate"} aid="endDate" label="To" />
              <Area id={p + "roleDescription"} aid="roleDescription" label="Role Description" maxLength={2000} />
            </div>
          );
        })}
        <AddButton onClick={() => setJobs(jobs + 1)} label={jobs ? "Add Another" : "Add"} />
      </Section>
      <Section name="Education">
        {nums(schools).map((n) => {
          const p = `education-${n}--`;
          return (
            <div key={n} role="group" aria-labelledby={`Education-${n}-panel`}>
              <h5 id={`Education-${n}-panel`}>Education {n}</h5>
              <Text id={p + "schoolName"} aid="schoolName" label="School or University" required />
              <ListButton id={p + "degree"} aid="degree" label="Degree" options={["Not Completed", "High School", "AA", "AS", "Bachelor's", "MA", "MS", "MBA", "PhD"]} required />
              <Prompt id={p + "fieldOfStudy"} aid="fieldOfStudy" label="Field of Study" options={["Computer Engineering", "Computer Science", "Mathematics"]} />
              <Text id={p + "gradeAverage"} aid="gradeAverage" label="Overall Result (GPA)" />
              <DateParts id={p + "firstYearAttended"} aid="firstYearAttended" label="From" month={false} />
              <DateParts id={p + "lastYearAttended"} aid="lastYearAttended" label="To (Actual or Expected)" month={false} />
            </div>
          );
        })}
        <AddButton onClick={() => setSchools(schools + 1)} label={schools ? "Add Another" : "Add"} />
      </Section>
    </>
  );
}

function Step({ n }) {
  if (n === 0) return (
    <>
      <Prompt id="source--source" aid="source" label="How Did You Hear About Us?" browse required
        options={[["Advertising", ["Indeed", "Handshake"]], ["Referral", ["Employee Referral"]], ["Social Network", ["Facebook", "LinkedIn", "Twitter"]], ["Website", ["Company Website"]]]} />
      <Radio id="previousWorker" label="Have you previously worked for Mock Co?" options={YN} />
      <ListButton id="country--country" aid="country" label="Country" options={["Canada", "India", "United Kingdom", "United States of America"]} required />
      <Text id="name--legalName--firstName" aid="legalName--firstName" label="First Name" required />
      <Text id="name--legalName--middleName" aid="legalName--middleName" label="Middle Name" />
      <Text id="name--legalName--lastName" aid="legalName--lastName" label="Last Name" required />
      <Text id="address--addressLine1" aid="addressLine1" label="Address Line 1" required />
      <Text id="address--addressLine2" aid="addressLine2" label="Address Line 2" />
      <Text id="address--city" aid="city" label="City" required />
      <ListButton id="address--countryRegion" aid="countryRegion" label="State" options={["Massachusetts", "New York", "Texas"]} required />
      <Text id="address--postalCode" aid="postalCode" label="Postal Code" required />
      <Text id="emailAddress--emailAddress" aid="email" label="Email Address" required type="email" />
      <ListButton id="phoneNumber--phoneType" aid="phoneType" label="Phone Device Type" options={["Fax", "Mobile", "Pager", "Telephone"]} required />
      <Prompt id="phoneNumber--countryPhoneCode" aid="countryPhoneCode" label="Country Phone Code" required
        options={["India (+91)", "United States Minor Outlying Islands (+1)", "United States of America (+1)"]} />
      <Text id="phoneNumber--phoneNumber" aid="phoneNumber" label="Phone Number" required type="tel" />
      <Text id="phoneNumber--extension" aid="extension" label="Phone Extension" />
    </>
  );
  if (n === 1) return (
    <>
      <WorkHistory />
      <Skills />
      <Languages />
      <Resume />
      <Text id="socialNetworkAccounts--linkedInAccount" aid="linkedInAccount" inputAid="linkedinQuestion" label="LinkedIn" />
      {EMPTY ? <><Websites /><Projects /><Certifications /></> : <Text id="webAddress-1--url" aid="url" inputAid="website" label="URL" />}
    </>
  );
  if (n === 2) return (
    <>
      <ListButton id="q1" aid="q1" label="Are you legally authorized to work in the United States?" options={YN} required />
      <ListButton id="q2" aid="q2" label="Will you now or in the future require sponsorship for employment visa status (e.g., H-1B visa status)?" options={YN} required />
      <ListButton id="q3" aid="q3" label="Are you willing to relocate?" options={YN} required />
      <ListButton id="q4" aid="q4" label="Are you comfortable working on call one week per month?" options={YN} required />
    </>
  );
  if (n === 3) return (
    <>
      <ListButton id="gender" aid="gender" label="Please select your gender." options={["Male", "Female", "Not Declared"]} required />
      <ListButton id="ethnicity" aid="ethnicity" buttonAid="ethnicityDropdown" label="Please select the ethnicity which most accurately describes how you identify yourself." options={["Asian (United States of America)", "White (United States of America)", "I do not wish to answer."]} required />
      <ListButton id="veteran" aid="veteranStatus" label="Please select the veteran status which most accurately describes your relationship with the military" options={["I am not a protected veteran", "I identify as one or more of the classifications of protected veteran", "I do not wish to self-identify"]} required />
      <Agreement />
    </>
  );
  if (n === 4) return (
    <>
      <h3>Voluntary Self-Identification of Disability</h3>
      <ListButton id="selfIdLanguage" aid="language" label="Language" options={["English", "Spanish"]} required />
      <Text id="selfIdentifiedDisabilityData--name" aid="name" label="Name" required />
      <DateParts id="selfIdentifiedDisabilityData--dateSignedOn" aid="dateSignedOn" label="Date" day />
      <div data-automation-id="formField-disabilityStatus">
        <fieldset>
          <legend><label>Please check one of the boxes below:</label></legend>
          {["Yes, I have a disability (or previously had a disability)", "No, I do not have a disability and have not had one in the past", "I do not want to answer"].map((o, i) => (
            <Check key={o} id={"disability-" + i} aid={"disability-" + i} label={o} />
          ))}
        </fieldset>
      </div>
    </>
  );
  return <p>Review your application.</p>;
}

function App() {
  const [n, setN] = useState(0);
  return (
    <div data-automation-id="applyFlowPage">
      <ol data-automation-id="progressBar">{STEPS.map((s, i) => <li key={s} data-automation-id={i === n ? "progressBarActiveStep" : "progressBarInactiveStep"}>{s}</li>)}</ol>
      <h2 data-automation-id="pageHeaderText">{STEPS[n]}</h2>
      <div key={n}><Step n={n} /></div>
      <div data-automation-id="pageFooter">
        {n > 0 ? <button type="button" data-automation-id="pageFooterBackButton" onClick={() => { window.__navClicks++; setN(n - 1); }}>Back</button> : null}
        <button type="button" data-automation-id="bottom-navigation-next-button" onClick={() => { window.__navClicks++; if (n === STEPS.length - 1) window.__submitted++; else setN(n + 1); }}>
          {n === STEPS.length - 1 ? "Submit" : "Save and Continue"}</button>
      </div>
    </div>
  );
}

exposeState(S);
createRoot(document.getElementById("root")).render(<App />);
