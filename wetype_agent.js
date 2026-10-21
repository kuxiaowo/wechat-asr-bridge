'use strict';
// Python resolves these encoder addresses from unique executable signatures.
const targetModule = Process.getModuleByName('wetype_update.exe');
const base = targetModule.base;
function native(rva, ret, args) {
 return new NativeFunction(base.add(rva), ret, args, {abi:'win64', exceptions:'steal'});
}
const construct = native(0xb2c090, 'pointer', ['pointer']);
const initialize = native(0xb2c350, 'int', ['pointer','int','int','int','int']);
const encode = native(0xb2c110, 'int', ['pointer','pointer','int','pointer','int','pointer']);
const release = native(0xb2c0d0, 'void', ['pointer']);
const business = Process.getModuleByName('wetap_business.dll');
const submit = new NativeFunction(business.getExportByName('business_net_start_task_with_option'),
 'int', ['int','int','pointer','uint','pointer','pointer','pointer'], {abi:'win64', exceptions:'steal'});
const tasks = new Map();
let closing = false;
let callbacks = 0;
const callback = new NativeCallback((user, taskId, code, response, length) => {
 callbacks++;
 const key = user.toString(), task = tasks.get(key);
 if (!task) { callbacks--; return; }
 let result;
 try {
  if (length > 4 * 1024 * 1024 || (length && response.isNull())) throw Error('Invalid native response length');
  result = {taskId, code, data:length ? Array.from(new Uint8Array(response.readByteArray(length))) : []};
 } catch (error) { result = {taskId, code:-1, error:String(error), data:[]}; }
 clearTimeout(task.timeout);
 // Resolve after returning to the native caller. The shared callback stays
 // pinned for the lifetime of the agent, including on timed-out requests.
 setTimeout(() => {
  tasks.delete(key);
  callbacks--;
  task.resolve(result);
 }, 250);
}, 'void', ['pointer','int','int','pointer','uint'], 'win64');

rpc.exports = {
 encode(pcm) {
  if (closing || pcm.length === 0 || pcm.length % 640 !== 0) throw Error('Invalid PCM or agent stopping');
  const object = Memory.alloc(0x28), input = Memory.alloc(pcm.length);
  const capacity = pcm.length / 640 * 1502 + 16;
  const output = Memory.alloc(capacity), length = Memory.alloc(4);
  input.writeByteArray(pcm);
  construct(object);
  try {
   let code = initialize(object, 16000, 1, 64000, 1);
   if (code !== 0) throw Error('Opus initialization failed: ' + code);
   code = encode(object, input, pcm.length, output, capacity, length);
   if (code !== 0) throw Error('Opus encoding failed: ' + code);
   const size = length.readU32();
   if (size > capacity) throw Error('Invalid Opus output size');
   return Array.from(new Uint8Array(output.readByteArray(size)));
  } finally { release(object); }
 },
 request(bytes, owner = '') {
  if (closing || !bytes.length) throw Error('Invalid request or agent stopping');
  return new Promise((resolve, reject) => {
   const data = Memory.alloc(bytes.length), options = Memory.alloc(16), user = Memory.alloc(8);
   data.writeByteArray(bytes);
   options.writeByteArray(new Uint8Array(16));
   options.writeU32(3000);
   options.add(4).writeU8(2);
   const key = user.toString(), task = {data, options, user, resolve, reject, owner};
   tasks.set(key, task);
   task.timeout = setTimeout(() => {
    // Do not release native callback/user data without acknowledgement.
    reject(Error('Native task timed out; callback retained'));
   }, 15000);
   try {
    const id = submit(4548, 0, data, bytes.length, user, callback, options);
    if (id < 0) {
     clearTimeout(task.timeout);
     tasks.delete(key);
     resolve({taskId:id, code:id, data:[]});
    }
   } catch (error) {
    // An exception may occur after registration; retain ownership until exit.
    clearTimeout(task.timeout);
    reject(error);
   }
  });
 },
 stats() { return {pending:tasks.size, callbacks, closing}; },
 finalize(owner) {
  // A live task belonging to another audio must not quarantine this caller.
  return {cleaned: owner === undefined ? tasks.size === 0 && callbacks === 0 :
   !Array.from(tasks.values()).some(task => task.owner === owner)};
 },
 dispose() {
  closing = true;
  return {cleaned:tasks.size === 0 && callbacks === 0};
 }
};
