// Mock of a Greenhouse job-boards application form (Remix + React 18 +
// react-select with classNamePrefix "select"), modelled on the live markup
// of job-boards.greenhouse.io in October 2026. Live boards open their
// dropdowns from a mouseup handler on a wrapper, not react-select's own
// mousedown, so this mock does the same.
import React, { useState, useRef } from "react";
import { createRoot } from "react-dom/client";
import Select from "react-select";
import AsyncSelect from "react-select/async";
import { exposeState } from "./common.js";

const YES_NO = [{ value: 0, label: "No" }, { value: 1, label: "Yes" }];
const COUNTRIES = ["Canada", "India", "Mexico", "United Kingdom", "United States", "United States Minor Outlying Islands"].map((c, i) => ({ value: i, label: c }));
const SCHOOLS = ["Boston College", "Boston University", "Northeastern University", "Northwestern University", "Tufts University"];
const DEGREES = ["High School", "Associate's Degree", "Bachelor's Degree", "Master's Degree", "Doctor of Philosophy (Ph.D.)"].map((d, i) => ({ value: i, label: d }));
const GENDER = ["Male", "Female", "Decline To Self Identify"].map((d, i) => ({ value: i, label: d }));
const RACE = ["American Indian or Alaskan Native", "Asian", "Black or African American", "Hispanic or Latino", "White", "Two or More Races", "Decline To Self Identify"].map((d, i) => ({ value: i, label: d }));
const VETERAN = ["I am a veteran", "I am not a veteran", "I don't wish to answer"].map((d, i) => ({ value: i, label: d }));
const DISABILITY = ["Yes, I have a disability (or previously had a disability)", "No, I do not have a disability and have not had one in the past", "I do not want to answer"].map((d, i) => ({ value: i, label: d }));
const NONCOMPETE = ["Yes", "No"].map((d, i) => ({ value: i, label: d }));

const S = {};

function TextField({ id, label, required, type = "text", maxLength }) {
  const [v, setV] = useState("");
  S[id] = v;
  return (
    <div className="text-input-wrapper"><div className="input-wrapper">
      <label id={id + "-label"} htmlFor={id} className="label">{label}{required && <span aria-hidden="true">*</span>}</label>
      <input id={id} className="input input__single-line" type={type} aria-required={required ? "true" : "false"} maxLength={maxLength}
        value={v} onChange={(e) => setV(e.target.value)} />
    </div></div>
  );
}

function GhSelect({ id, label, options, multi, async: isAsync }) {
  const [open, setOpen] = useState(false);
  const [v, setV] = useState(multi ? [] : null);
  S[id] = multi ? v.map((o) => o.label) : v && v.label;
  const common = {
    inputId: id, instanceId: id, classNamePrefix: "select", className: "select-shell", isMulti: multi,
    value: v, onChange: (o) => { setV(o); if (!multi) setOpen(false); },
    menuIsOpen: open, onMenuClose: () => setOpen(false), openMenuOnClick: false,
    "aria-labelledby": id + "-label", placeholder: "Select...",
  };
  return (
    <div className="select"><div className="select__container">
      <label id={id + "-label"} htmlFor={id} className="label select__label">{label}<span aria-hidden="true">*</span></label>
      <div onMouseUp={(e) => { if (!e.target.closest(".select__menu")) setOpen((o) => !o); }} onKeyDown={(e) => { if (e.key === "ArrowDown") setOpen(true); }}>
        {isAsync
          ? <AsyncSelect {...common} cacheOptions loadOptions={(q) => new Promise((r) => setTimeout(() => r(SCHOOLS.filter((s) => s.toLowerCase().includes(q.toLowerCase())).map((s, i) => ({ value: i, label: s }))), 300))} />
          : <Select {...common} options={options} />}
      </div>
      <input required tabIndex={-1} aria-hidden="true" className="remix-css-requiredInput" value={multi ? v.length ? "x" : "" : v ? "x" : ""} onChange={() => {}} />
    </div></div>
  );
}

function FileField({ id, label }) {
  const [name, setName] = useState("");
  S[id] = name;
  const ref = useRef();
  return (
    <div className="field-wrapper"><div role="group" aria-labelledby={"upload-label-" + id} className="file-upload">
      <div id={"upload-label-" + id} className="label upload-label">{label}<span className="required">*</span></div>
      <div className="file-upload__wrapper">
        {name ? <div className="file-upload__filename">{name}</div> : null}
        <div className="button-container"><div className="secondary-button"><div>
          <button type="button" className="btn btn--pill" onClick={() => ref.current.click()}>Attach</button>
          <label className="visually-hidden" htmlFor={id}>Attach</label>
          <input ref={ref} id={id} className="visually-hidden" type="file" accept=".pdf,.doc,.docx,.txt,.rtf"
            onChange={(e) => setName(e.target.files[0] ? e.target.files[0].name + ":" + e.target.files[0].size : "")} />
        </div></div>
        <div className="secondary-button"><button type="button" className="btn btn--pill">Dropbox</button></div></div>
      </div>
    </div></div>
  );
}

function Location() {
  const [v, setV] = useState("");
  const [open, setOpen] = useState(false);
  S.location = v;
  const opts = v.length >= 3 ? ["Boston, Massachusetts, United States", "Boston, Lincolnshire, United Kingdom"].filter((o) => o.toLowerCase().startsWith(v.toLowerCase().split(",")[0])) : [];
  return (
    <div className="input-wrapper">
      <label htmlFor="candidate-location" className="label">Location (City)</label>
      <input id="candidate-location" role="combobox" aria-expanded={open && opts.length ? "true" : "false"} aria-controls="candidate-location-listbox"
        type="text" value={v} onChange={(e) => { setV(e.target.value); setOpen(true); }} />
      {open && opts.length ? (
        <ul id="candidate-location-listbox" role="listbox">
          {opts.map((o) => <li key={o} role="option" onClick={() => { setV(o); setOpen(false); }}>{o}</li>)}
        </ul>
      ) : null}
    </div>
  );
}

function Consent() {
  const [v, setV] = useState(false);
  S.consent = v;
  return (
    <div className="checkbox">
      <input type="checkbox" id="gdpr_1" name="gdpr" checked={v} onChange={(e) => setV(e.target.checked)} className="checkbox--full-width" />
      <label htmlFor="gdpr_1">By checking this box, I consent to Mock Co collecting, storing, and processing my responses to the demographic data surveys above.</label>
    </div>
  );
}

function Why() {
  const [v, setV] = useState("");
  S.why = v;
  return (
    <div className="text-input-wrapper"><div className="input-wrapper">
      <label id="question_9-label" htmlFor="question_9" className="label">Why do you want to work at Mock Co?</label>
      <textarea id="question_9" className="input" maxLength={600} value={v} onChange={(e) => setV(e.target.value)} />
    </div></div>
  );
}

function App() {
  const [, force] = useState(0);
  return (
    <form id="application-form" onSubmit={(e) => e.preventDefault()}>
      <h2>Apply for this job</h2>
      <TextField id="first_name" label="First Name" required />
      <TextField id="last_name" label="Last Name" required />
      <TextField id="preferred_name" label="Preferred First Name" required />
      <TextField id="email" label="Email" required />
      <GhSelect id="country" label="Country" options={COUNTRIES} />
      <TextField id="phone" label="Phone" type="tel" required />
      <Location />
      <FileField id="resume" label="Resume/CV" />
      <FileField id="cover_letter" label="Cover Letter" />
      <TextField id="question_1" label="LinkedIn Profile" required />
      <TextField id="question_2" label="Website" />
      <TextField id="question_3" label="How did you hear about this job?" required />
      <GhSelect id="question_4" label="Are you currently eligible to work legally in the United States of America?" options={YES_NO} />
      <GhSelect id="question_5" label="Do you now or in the future require immigration support or visa sponsorship to continue working in the United States?" options={YES_NO} />
      <GhSelect id="question_6" label="Do you acknowledge that this is a hybrid role based in Boston and you will be required to come into the office four days a week?" options={YES_NO} />
      <GhSelect id="question_7" label="Are you currently subject to any agreement with a former employer (such as a non-compete agreement) that may limit your ability to perform the duties of the position?" options={NONCOMPETE} />
      <TextField id="question_8" label="When can you start?" />
      <Why />
      <div className="education--form">
        <h3>Education</h3>
        <GhSelect id="school--0" label="School" async />
        <GhSelect id="degree--0" label="Degree" options={DEGREES} />
      </div>
      <div className="eeoc">
        <h3>Voluntary Self-Identification</h3>
        <GhSelect id="4024620002" label="I identify my gender as:" options={GENDER} />
        <GhSelect id="4024625002" label="I identify my race/ethnicity as (please mark all that apply):" options={RACE} multi />
        <GhSelect id="4024623002" label="Veteran Status:" options={VETERAN} />
        <GhSelect id="4024624002" label="I have a disability:" options={DISABILITY} />
        <GhSelect id="4024621002" label="I identify as:" options={[{ value: 0, label: "Transgender" }, { value: 1, label: "Cisgender" }]} />
      </div>
      <Consent />
      <button type="submit" className="btn btn--pill" onClick={() => force((x) => x + 1)}>Submit application</button>
    </form>
  );
}

exposeState(S);
createRoot(document.getElementById("root")).render(<App />);
