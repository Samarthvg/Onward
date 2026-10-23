// Mock of a Workday candidate application (*.myworkdayjobs.com .../apply),
// a React wizard whose fields carry data-automation-id names. Real Workday
// markup varies by tenant; this follows the commonly seen ids: legalNameSection_*,
// formField-* wrappers, listbox buttons with a popup menu, the multiselect
// search prompt (searchBox / promptOption) and file-upload-input-ref.
import React, { useState } from "react";
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

function ListButton({ id, aid, buttonAid, label, options, required }) {
  const [v, setV] = useState(S[id] || "");
  const [open, setOpen] = useState(false);
  S[id] = v;
  return (
    <Field id={id} aid={aid} label={label} required={required}>
      <button type="button" id={id} aria-haspopup="listbox" aria-expanded={open ? "true" : "false"} data-automation-id={buttonAid}
        onClick={() => setOpen((o) => !o)}>{v || "Select One"}</button>
      {open ? createPortal(
        <div data-automation-id="popUpDialog" style={{ position: "fixed", top: 40, left: 40, background: "#fff", border: "1px solid #999" }}>
          <ul role="listbox" aria-label={label}>
            {options.map((o) => (
              <li key={o} role="option" aria-selected={v === o ? "true" : "false"} onClick={() => { setV(o); setOpen(false); }}>
                <div data-automation-id="promptOption">{o}</div>
              </li>
            ))}
          </ul>
        </div>, document.body) : null}
    </Field>
  );
}

function Prompt({ id, aid, label, options, required, multi }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [chosen, setChosen] = useState(S[id] || []);
  S[id] = chosen;
  const hits = options.filter((o) => o.toLowerCase().includes(q.toLowerCase()));
  return (
    <Field id={id} aid={aid} label={label} required={required}>
      <div data-automation-id="multiselectInputContainer">
        {chosen.map((c) => <div key={c} data-automation-id="selectedItem">{c}</div>)}
        <input id={id} data-automation-id="searchBox" placeholder="Search" value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); setOpen(true); } if (e.key === "Escape") setOpen(false); }} />
      </div>
      {open ? createPortal(
        <div data-automation-id="activeListContainer" role="listbox">
          {hits.map((o) => (
            <div key={o} role="option" data-automation-id="promptLeafNode" onClick={() => { setChosen(multi ? (chosen.includes(o) ? chosen : [...chosen, o]) : [o]); setQ(""); setOpen(false); }}>
              <div data-automation-id="promptOption">{o}</div>
            </div>
          ))}
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

/** Workday's date widget: separate month and year spinbuttons under one label. */
function DateParts({ id, aid, label, month = true, day = false }) {
  const part = (p, name, size) => {
    const pid = `${id}-dateSection${p}-input`;
    const [v, setV] = useState(S[pid] || "");
    const [committed, setCommitted] = useState(S[pid] || "");
    S[pid] = committed;
    return <input id={pid} role="spinbutton" aria-label={name} data-automation-id={`dateSection${p}-input`} size={size} value={v}
      onChange={(e) => setV(e.target.value.replace(/\D/g, "").slice(0, size))} onBlur={(e) => setCommitted(e.target.value)} />;
  };
  return (
    <div data-automation-id={"formField-" + aid}>
      <label>{label}</label>
      <div data-automation-id="dateInputWrapper">{month ? part("Month", "Month", 2) : null}{day ? part("Day", "Day", 2) : null}{part("Year", "Year", 4)}</div>
    </div>
  );
}

const SKILL_LIST = ["Angular", "Express.js", "Java", "JavaScript", "Node.js", "Python", "React.js", "SQL", "TypeScript", "Kotlin", "Go"];

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
    <div {...(EMPTY ? {} : { "data-automation-id": "languageSection", role: "group", "aria-labelledby": "language-heading" })}>
      <h3 id="language-heading">Languages</h3>
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
      <button type="button" data-automation-id="Add" onClick={() => setN(n + 1)}>Add</button>
    </div>
  );
}

function Websites() {
  const [n, setN] = useState(S.__sites ?? 0);
  S.__sites = n;
  return (
    <div>
      <h3>Websites</h3>
      {Array.from({ length: n }, (_, i) => i + 1).map((k) => (
        <div key={k} data-automation-id={`webAddress-${k}`}>
          <Text id={`webAddress-${k}--url`} aid="url" label="URL" required />
        </div>
      ))}
      <button type="button" data-automation-id="Add" onClick={() => setN(n + 1)}>Add</button>
    </div>
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
      <button type="button" data-automation-id="Add" onClick={() => setN(n + 1)}>Add</button>
    </div>
  );
}

function Certifications() {
  const [n, setN] = useState(S.__certs ?? 0);
  S.__certs = n;
  return (
    <div>
      <h3>Certifications</h3>
      {Array.from({ length: n }, (_, i) => i + 1).map((k) => (
        <div key={k} data-automation-id={`certification-${k}`}>
          <Text id={`certification-${k}--certification`} aid="certification" label="Certification" />
        </div>
      ))}
      <button type="button" data-automation-id="Add" onClick={() => setN(n + 1)}>Add</button>
    </div>
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
      <div {...(EMPTY ? {} : { "data-automation-id": "workExperienceSection", role: "group", "aria-labelledby": "workExperience-heading" })}>
        <h3 id="workExperience-heading">Work Experience</h3>
        {nums(jobs).map((n) => {
          const p = `workExperience-${n}--`;
          return (
            <div key={n} data-automation-id={`workExperience-${n}`}>
              <h4>Work Experience {n}</h4>
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
        <button type="button" data-automation-id="Add" aria-label={EMPTY ? undefined : "Add Work Experience"} onClick={() => setJobs(jobs + 1)}>{EMPTY ? "Add" : "Add Another"}</button>
      </div>
      <div {...(EMPTY ? {} : { "data-automation-id": "educationSection", role: "group", "aria-labelledby": "education-heading" })}>
        <h3 id="education-heading">Education</h3>
        {nums(schools).map((n) => {
          const p = `education-${n}--`;
          return (
            <div key={n} data-automation-id={`education-${n}`}>
              <h4>Education {n}</h4>
              <Text id={p + "schoolName"} aid="schoolName" label="School or University" required />
              <ListButton id={p + "degree"} aid="degree" label="Degree" options={["High School Diploma", "Bachelor's Degree", "Master's Degree", "Doctorate"]} />
              <Prompt id={p + "fieldOfStudy"} aid="fieldOfStudy" label="Field of Study" options={["Computer Engineering", "Computer Science", "Mathematics"]} />
              <Text id={p + "gradeAverage"} aid="gradeAverage" label="Overall Result (GPA)" />
              <DateParts id={p + "firstYearAttended"} aid="firstYearAttended" label="From" month={false} />
              <DateParts id={p + "lastYearAttended"} aid="lastYearAttended" label="To (Actual or Expected)" month={false} />
            </div>
          );
        })}
        <button type="button" data-automation-id="Add" aria-label={EMPTY ? undefined : "Add Education"} onClick={() => setSchools(schools + 1)}>{EMPTY ? "Add" : "Add Another"}</button>
      </div>
    </>
  );
}

function Step({ n }) {
  if (n === 0) return (
    <>
      <Prompt id="source--source" aid="source" label="How Did You Hear About Us?" options={["Company Website", "Employee Referral", "Indeed", "LinkedIn"]} required />
      <Radio id="previousWorker" label="Have you previously worked for Mock Co?" options={YN} />
      <ListButton id="country--country" aid="country" buttonAid="countryDropdown" label="Country" options={["Canada", "India", "United Kingdom", "United States of America"]} required />
      <Text id="name--legalName--firstName" aid="legalName--firstName" inputAid="legalNameSection_firstName" label="First Name" required />
      <Text id="name--legalName--middleName" aid="legalName--middleName" inputAid="legalNameSection_middleName" label="Middle Name" />
      <Text id="name--legalName--lastName" aid="legalName--lastName" inputAid="legalNameSection_lastName" label="Last Name" required />
      <Text id="address--addressLine1" aid="addressLine1" inputAid="addressSection_addressLine1" label="Address Line 1" required />
      <Text id="address--city" aid="city" inputAid="addressSection_city" label="City" required />
      <Text id="address--postalCode" aid="postalCode" inputAid="addressSection_postalCode" label="Postal Code" required />
      <Text id="emailAddress--emailAddress" aid="email" inputAid="email" label="Email Address" required type="email" />
      <ListButton id="phoneNumber--phoneType" aid="phoneType" buttonAid="phone-device-type" label="Phone Device Type" options={["Mobile", "Home"]} required />
      <Prompt id="phoneNumber--countryPhoneCode" aid="countryPhoneCode" label="Country Phone Code" options={["United States of America (+1)", "India (+91)"]} required />
      <Text id="phoneNumber--phoneNumber" aid="phoneNumber" inputAid="phone-number" label="Phone Number" required type="tel" />
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
