// Firebase settings for the capture app.
//
// These values identify our Firebase project (senior-project-7c405). They are NOT secrets: every Firebase
// web app ships them to the browser, and the security rules (firestore.rules, storage.rules) are what
// protect the data. If the project ever changes, copy the new values from
//   Firebase console > Project settings (gear icon) > General > Your apps > Web app > SDK setup and configuration
export const firebaseConfig = {
  apiKey: "AIzaSyCFwAEOjfgd4OUHmt0JPy8vDpRMNwBXHao",
  authDomain: "senior-project-7c405.firebaseapp.com",
  projectId: "senior-project-7c405",
  storageBucket: "senior-project-7c405.firebasestorage.app",
  messagingSenderId: "947136096151",
};

// Local emulators or the real project?
//
// On the deployed site (*.web.app / *.firebaseapp.com) the app uses the real Firebase project.
// Anywhere else (http://localhost:5000, or http://<your PC's IP>:5000 from your phone) it uses the
// emulators started with `firebase emulators:start`, on the same machine that served the page.
// To test the deployed site against the emulators, or the reverse, add ?emulators=1 or ?emulators=0 to the URL.
const forced = new URLSearchParams(location.search).get("emulators");
const deployed = /\.(web\.app|firebaseapp\.com)$/.test(location.hostname);
export const useEmulators = forced !== null ? forced === "1" : !deployed;

// Ports must match the "emulators" section of firebase/firebase.json
export const emulatorHost = location.hostname || "127.0.0.1";
export const emulatorPorts = { auth: 9099, firestore: 8080, storage: 9199 };
