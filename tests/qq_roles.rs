use mutsumi_runtime::{
    napcat::NapcatConfig,
    web::roles::{RoleBook, Update},
};
use serde_json::json;

#[test]
fn changing_owner_never_reassigns_the_previous_media_audience() {
    let config = NapcatConfig {
        base_url: "http://127.0.0.1:9".into(),
        webui_url: "http://127.0.0.1:9".into(),
        token_env: "unused".into(),
        webui_token_env: "unused".into(),
        bot_id: "10101".into(),
        owner_id: "20202".into(),
        targets: Default::default(),
    };
    let original = RoleBook::seed(Some(&config));
    let update:Update=serde_json::from_value(json!({"version":1,"scope":"registered","contacts":[{"qq_id":"20202","name":"原主人","role":"friend","enabled":false},{"qq_id":"30303","name":"新主人","role":"owner","enabled":true}]})).unwrap();
    let next = original.updated(update, "10101").unwrap();
    assert_eq!(next.target("20202"), "qq_owner");
    assert_eq!(next.target("30303"), "qq_private_30303");
    assert!(!next.bindings().contains_key("qq_owner"));
    assert_eq!(next.bindings()["qq_private_30303"].id, "30303");
    let invalid:Update=serde_json::from_value(json!({"version":2,"scope":"owner","contacts":[{"qq_id":"10101","name":"机器人","role":"owner","enabled":true}]})).unwrap();
    assert!(next.updated(invalid, "10101").is_err());
}
