// Mock of an Ashby hosted application form (jobs.ashbyhq.com/<org>/<id>/application),
// React 18, modelled on the live markup in October 2026.
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { exposeState } from "./common.js";

const S = {};

function Entry({ path, label, required, description, children }) {
  return (
    <div className="_fieldEntry_1e3gg_28 ashby-application-form-field-entry" data-field-path={path}>
      <label className={"_heading_f7cvd_52 _label_1e3gg_42 ashby-application-form-question-title" + (required ? " _required_f7cvd_91" : "")} htmlFor={path}>{label}</label>
      {description ? <div className="ashby-application-form-question-description"><p>{description}</p></div> : null}
      {children}
    </div>
  );
}

function Text({ path, label, type = "text", required, textarea, placeholder = "Type here..." }) {
  const [v, setV] = useState("");
  S[path] = v;
  const props = { id: path, name: path, placeholder, value: v, onChange: (e) => setV(e.target.value), required };
  return (
    <Entry path={path} label={label} required={required}>
      {textarea ? <textarea className="ashby-application-form-input-textarea" rows={4} {...props} />
        : <input type={type} className="ashby-application-form-input-text" {...props} />}
    </Entry>
  );
}

function YesNo({ path, label, description }) {
  const [v, setV] = useState(null);
  S[path] = v;
  return (
    <Entry path={path} label={label} required description={description}>
      <div className="_container_1svni_28 _yesno_1e3gg_148 ashby-application-form-input-yesno">
        {["yes", "no"].map((o) => (
          <button key={o} type="button" className="ashby-application-form-input-yesno-option" aria-pressed={v === o ? "true" : "false"} data-option={o}
            onClick={() => setV(o)}>{o === "yes" ? "Yes" : "No"}</button>
        ))}
        <input type="checkbox" className="_input_1svni_78" tabIndex={-1} name={path} checked={v === "yes"} onChange={() => {}} />
      </div>
    </Entry>
  );
}

function FileInput({ path, label }) {
  const [name, setName] = useState("");
  S[path] = name;
  return (
    <Entry path={path} label={label} required>
      <div role="presentation" className="ashby-application-form-input-file">
        <input type="file" tabIndex={-1} id={path} required accept="application/pdf,.pdf,application/msword,.doc,.docx,.odt,.rtf,.txt"
          style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0,0,0,0)" }}
          onChange={(e) => setName(e.target.files[0] ? e.target.files[0].name + ":" + e.target.files[0].size : "")} />
        <div className="ashby-application-form-input-file-dropzone">
          {name ? <span className="_filename">{name.split(":")[0]}</span> : <button type="button">Upload File</button>}
        </div>
      </div>
    </Entry>
  );
}

const PLACES = ["Boston, Massachusetts, United States", "Boston, England, United Kingdom", "Brooklyn, New York, United States"];
function Location() {
  const [v, setV] = useState("");
  const [open, setOpen] = useState(false);
  const [chosen, setChosen] = useState("");
  S._systemfield_location = chosen;
  const hits = v ? PLACES.filter((p) => p.toLowerCase().startsWith(v.toLowerCase().split(",")[0].trim())) : [];
  return (
    <Entry path="_systemfield_location" label="Location">
      <div className="_inputContainer_d7ago_28">
        <input className="ashby-application-form-input-autocomplete" placeholder="Start typing..." aria-autocomplete="list" aria-expanded={open ? "true" : "false"}
          aria-haspopup="listbox" role="combobox" value={v} onChange={(e) => { setV(e.target.value); setOpen(true); setChosen(""); }} />
        {open && hits.length ? (
          <div role="listbox" className="_results">
            {hits.map((h) => <div key={h} role="option" aria-selected="false" onClick={() => { setV(h); setChosen(h); setOpen(false); }}>{h}</div>)}
          </div>
        ) : null}
      </div>
    </Entry>
  );
}

function Radios({ path, label, options }) {
  const [v, setV] = useState("");
  S[path] = v;
  return (
    <div className="ashby-application-form-field-entry" data-field-path={path}>
      <fieldset>
        <legend className="ashby-application-form-question-title">{label}</legend>
        {options.map((o, i) => (
          <div key={o}><input type="radio" id={path + "-" + i} name={path} value={o} checked={v === o} onChange={() => setV(o)} /><label htmlFor={path + "-" + i}>{o}</label></div>
        ))}
      </fieldset>
    </div>
  );
}

function App() {
  return (
    <div id="form" role="tabpanel">
      <div className="_autofillPane_5yu8i_448 ashby-application-form-autofill-pane">
        <div className="ashby-application-form-autofill-input-root"><input type="file" tabIndex={-1} onChange={() => { S.autofillPane = "touched"; }} />
          <h3>Autofill from resume</h3><button type="button">Upload file</button></div>
      </div>
      <div className="_jobPostingForm_5yu8i_402 ashby-application-form-container">
        <div className="ashby-application-form-section-container">
          <Text path="_systemfield_name" label="Name" required />
          <Text path="_systemfield_email" label="Email" type="email" required placeholder="hello@example.com..." />
          <Text path="74f31fc6" label="LinkedIn" required />
          <FileInput path="_systemfield_resume" label="Resume" />
          <Text path="cadd1ed8" label="Phone Number" type="tel" required placeholder="1-415-555-1234..." />
          <Location />
          <Text path="4645121d" label="How many years of industry experience do you have?" type="number" required />
          <YesNo path="56f07ce0" label="Are you able to work from our SF office five days per week?" />
          <YesNo path="086af02e" label="Will you now or in the future require sponsorship for employment visa status in the United States?"
            description="We may provide immigration and sponsorship support to new employees based on their unique circumstances." />
          <Radios path="team" label="Which team interests you most?" options={["Platform", "Product", "Infrastructure"]} />
          <Text path="d9d19e65" label="Additional Information" textarea />
          <Text path="cd46f09e" label="What's the most interesting paper, blog post, or documentation you've read in the past month?" textarea required />
        </div>
        <div className="ashby-application-form-section-container">
          <h2>U.S. Equal Employment Opportunity Information</h2>
          <Radios path="eeo_gender" label="Gender" options={["Male", "Female", "Decline to self-identify"]} />
          <Radios path="eeo_veteran" label="Veteran Status" options={["I am a protected veteran", "I am not a protected veteran", "I don't wish to answer"]} />
        </div>
        <button type="submit" className="ashby-application-form-submit-button" onClick={() => { window.__submitted++; }}><span>Submit Application</span></button>
      </div>
    </div>
  );
}

exposeState(S);
createRoot(document.getElementById("root")).render(<App />);
