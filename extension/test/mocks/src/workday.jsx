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
const STEPS = ["My Information", "My Experience", "Application Questions", "Voluntary Disclosures", "Review"];

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

function Prompt({ id, aid, label, options, required }) {
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
            <div key={o} role="option" data-automation-id="promptLeafNode" onClick={() => { setChosen([o]); setQ(""); setOpen(false); }}>
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
      <Resume />
      <Text id="socialNetworkAccounts--linkedInAccount" aid="linkedInAccount" inputAid="linkedinQuestion" label="LinkedIn" />
      <Text id="webAddress-1--url" aid="url" inputAid="website" label="URL" />
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
      <ListButton id="gender" aid="gender" label="Gender" options={["Male", "Female", "I do not wish to answer"]} required />
      <ListButton id="ethnicity" aid="ethnicity" buttonAid="ethnicityDropdown" label="Ethnicity" options={["Asian (United States of America)", "White (United States of America)", "I do not wish to answer."]} required />
      <ListButton id="veteran" aid="veteranStatus" label="Veteran Status" options={["I am not a protected veteran", "I identify as one or more of the classifications of protected veteran", "I do not wish to self-identify"]} required />
      <Agreement />
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
