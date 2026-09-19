import { registerWebModule, NativeModule } from 'expo';

class AriaNativeModule extends NativeModule<{}> {}

export default registerWebModule(AriaNativeModule, 'AriaNativeModule');
